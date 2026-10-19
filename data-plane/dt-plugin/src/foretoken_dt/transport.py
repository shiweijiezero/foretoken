# SPDX-License-Identifier: Apache-2.0
# SPDX-FileCopyrightText: Copyright contributors to the Foretoken project

"""Worker-owned tensor buffers and pinned Mooncake device transports."""

from __future__ import annotations

import asyncio
import ctypes
import json
import math
import os
import time
from collections import defaultdict
from dataclasses import dataclass
from typing import Any
from uuid import uuid4

import torch

# Registrations must outlive Python callers, including cancelled coroutines.
# Only explicit, successful close removes this process-owned reference. Uncertain
# DMA completion requires terminating the owning process, never reusing its tensor.
_live_transports: set[TensorTransport] = set()

# Wire names are explicit: Python attribute lookup must not define the protocol.
_TENSOR_DTYPES = {
    "bool": torch.bool,
    "uint8": torch.uint8,
    "int8": torch.int8,
    "int16": torch.int16,
    "int32": torch.int32,
    "int64": torch.int64,
    "float16": torch.float16,
    "bfloat16": torch.bfloat16,
    "float32": torch.float32,
    "float64": torch.float64,
}


@dataclass(frozen=True)
class PayloadRef:
    """Immutable single-consumer publication; the producer owns its registration."""

    publication_id: str
    segment: str
    address: int | None
    nbytes: int
    dtype: str
    shape: tuple[int, ...]
    transport: str

    def __post_init__(self) -> None:
        """Validate dense tensor layout before allocating or submitting a read."""
        if self.transport in ("rdma", "gpu_ipc"):
            if type(self.address) is not int or self.address <= 0:
                raise ValueError("device address must be a positive integer")
        elif self.transport == "zmq":
            if self.address is not None or not self.segment.startswith(
                ("ipc://", "tcp://")
            ):
                raise ValueError(
                    "ZMQ publications require a socket endpoint, without an address"
                )
        else:
            raise ValueError("unsupported tensor transport")
        if self.dtype not in _TENSOR_DTYPES:
            raise ValueError("unsupported tensor dtype")
        if any(type(size) is not int or size <= 0 for size in self.shape):
            raise ValueError("tensor dimensions must be positive integers")
        expected = (
            math.prod(self.shape)
            * torch.empty((), dtype=_TENSOR_DTYPES[self.dtype]).element_size()
        )
        if self.nbytes != expected:
            raise ValueError("tensor layout does not match payload byte size")

    def to_wire(self) -> dict[str, Any]:
        """Encode only the explicitly supported transport descriptor fields."""
        return {
            "publication_id": self.publication_id,
            "segment": self.segment,
            "address": self.address,
            "nbytes": self.nbytes,
            "dtype": self.dtype,
            "shape": list(self.shape),
            "transport": self.transport,
        }

    @classmethod
    def from_wire(cls, value: dict[str, Any]) -> PayloadRef:
        """Decode a descriptor received from a trusted role control connection."""
        publication_id = value["publication_id"]
        segment = value["segment"]
        address = value.get("address")
        nbytes = value["nbytes"]
        if not isinstance(publication_id, str) or not publication_id:
            raise ValueError("publication_id must be a nonempty string")
        if not isinstance(segment, str) or not segment:
            raise ValueError("segment must be a nonempty string")
        if type(nbytes) is not int or nbytes <= 0:
            raise ValueError("nbytes must be a positive integer")
        dtype = value["dtype"]
        shape = value["shape"]
        if not isinstance(dtype, str) or not isinstance(shape, list):
            raise TypeError("tensor layout requires a dtype name and shape array")
        return cls(
            publication_id,
            segment,
            address,
            nbytes,
            dtype,
            tuple(shape),
            value["transport"],
        )


class TensorBuffer:
    """Retain reusable storage through publication, transfer and local GPU consumption."""

    def __init__(self, owner: TensorTransport, tensor: torch.Tensor):
        """Retain one allocation until its transport explicitly closes the buffer."""
        self.owner = owner
        self.tensor = tensor
        self.publication: PayloadRef | None = None
        self.transfer: asyncio.Task | None = None
        self.failed = False
        self.closed = False

    async def publish(
        self, *, ready_event: torch.cuda.Event | None = None, rows: int | None = None
    ) -> PayloadRef:
        """Fence producer writes and publish storage until explicit source release.

        ready_event must already be recorded after every local use of this
        buffer. Without an event, the entire device is synchronized.
        rows publishes only the leading rows while retaining the full registration.
        """
        self.owner.require_accepting()
        self._require_idle()
        self.transfer = asyncio.create_task(self._publish(ready_event, rows))
        return await asyncio.shield(self.transfer)

    async def _publish(
        self, ready_event: torch.cuda.Event | None, rows: int | None
    ) -> PayloadRef:
        """Retain a host-staging copy even if its initiating caller is cancelled."""
        await self._wait_device(ready_event)
        self.owner.require_accepting()
        tensor = self.tensor
        if rows is not None:
            if not tensor.ndim or not 0 < rows <= tensor.shape[0]:
                raise ValueError("publication rows exceed registered capacity")
            tensor = tensor[:rows]
        self.failed = True
        self.publication = await self.owner.publish(tensor)
        self.failed = False
        return self.publication

    def release_source(self, publication_id: str) -> None:
        """Release with proof of no future read: reader ACK or unclaimed revocation."""
        if (
            self.publication is None
            or self.publication.publication_id != publication_id
        ):
            raise ValueError("acknowledgement does not match the live publication")
        self.owner.release(publication_id)
        self.publication = None

    async def read(
        self,
        source: PayloadRef,
        *,
        ready_event: torch.cuda.Event | None = None,
        timing: dict | None = None,
    ) -> None:
        """Pull into this buffer; cancellation keeps transfer and memory ownership alive.

        The caller acknowledges the remote publication only after successful
        completion. A cancelled caller can await this buffer's transfer to obtain
        that result; cancellation never releases the producer automatically.
        ready_event fences previous destination use before the transfer overwrite;
        omit it to synchronize the device. Await read before launching consumers.
        The destination may have spare row capacity; only source.nbytes is written.
        """
        self.owner.require_accepting()
        self._require_idle()
        nbytes = self.tensor.numel() * self.tensor.element_size()
        if (
            source.nbytes > nbytes
            or _TENSOR_DTYPES[source.dtype] != self.tensor.dtype
            or len(source.shape) != self.tensor.ndim
            or source.shape[1:] != tuple(self.tensor.shape[1:])
        ):
            raise ValueError("source layout must fit the destination's row capacity")
        self.transfer = asyncio.create_task(self._read(source, ready_event, timing))
        await asyncio.shield(self.transfer)

    async def _read(
        self,
        source: PayloadRef,
        ready_event: torch.cuda.Event | None,
        timing: dict | None,
    ) -> None:
        """Fence old consumers and retain failed or cancelled transfer ownership."""
        # The destination may have been used by the previous GPU computation.
        started = time.monotonic()
        await self._wait_device(ready_event)
        ready = time.monotonic()
        self.failed = True
        await self.owner.read(self.tensor, source)
        self.failed = False
        if timing is not None:
            timing["read_fence_seconds"] = ready - started
            timing["transport_read_seconds"] = time.monotonic() - ready

    def _require_idle(self) -> None:
        """Reject reuse while exported, transferring, closed, or quarantined."""
        if self.closed or self.failed:
            raise RuntimeError("buffer is closed or quarantined")
        if self.publication is not None:
            raise RuntimeError("buffer is still published to a remote reader")
        if self.transfer is not None and not self.transfer.done():
            raise RuntimeError("buffer has an outstanding transfer")

    async def _wait_device(self, ready_event: torch.cuda.Event | None) -> None:
        """Fence all caller-owned GPU uses before publication, overwrite or cleanup.

        The caller records the event after joining every stream using this storage
        and must not re-record it or start new uses until the operation completes.
        This fence does not establish remote transfer completion.
        """
        if ready_event is not None:
            if not self.tensor.is_cuda or ready_event.device != self.tensor.device:
                raise ValueError("ready_event must be recorded on the tensor's device")
            await asyncio.to_thread(ready_event.synchronize)
        elif self.tensor.is_cuda:
            await asyncio.to_thread(torch.cuda.synchronize, self.tensor.device)

    async def close(self, *, ready_event: torch.cuda.Event | None = None) -> None:
        """Release transport registration only after publication and local use end."""
        if self.closed:
            return
        self._require_idle()
        await self._wait_device(ready_event)
        self._require_idle()
        self.failed = True
        self.owner.unregister(self.tensor)
        self.failed = False
        self.closed = True
        self.owner.buffers.remove(self)


class TensorTransport:
    """Own Worker buffer reuse and drain across device and host-staged transports."""

    def __init__(self):
        """Retain process ownership until a successful explicit drain."""
        self.buffers: set[TensorBuffer] = set()
        self.draining = False
        self.closed = False
        _live_transports.add(self)

    def register(self, tensor: torch.Tensor) -> TensorBuffer:
        """Retain contiguous storage until explicit close; the caller owns GPU streams."""
        self.require_accepting()
        if tensor.dtype not in _TENSOR_DTYPES.values():
            raise ValueError("unsupported tensor dtype")
        if not tensor.is_contiguous() or tensor.numel() == 0:
            raise ValueError("transport requires a nonempty contiguous tensor")
        if tensor.device.type not in ("cpu", "cuda"):
            raise ValueError("transport supports CPU and CUDA tensors")
        start = tensor.data_ptr()
        size = tensor.numel() * tensor.element_size()
        for buffer in self.buffers:
            if (
                start < buffer.tensor.data_ptr() + buffer.tensor.nbytes
                and buffer.tensor.data_ptr() < start + size
            ):
                raise ValueError("tensor overlaps an existing registration")
        self.register_memory(tensor)
        buffer = TensorBuffer(self, tensor)
        self.buffers.add(buffer)
        return buffer

    def register_memory(self, tensor: torch.Tensor) -> None:
        """Host-staged transports require no native device-memory registration."""

    def unregister(self, tensor: torch.Tensor) -> None:
        """Host-staged transports release storage with the owning TensorBuffer."""

    def release(self, publication_id: str) -> None:
        """Direct RDMA publications have no separately staged host snapshot."""

    async def publish(self, tensor: torch.Tensor) -> PayloadRef:
        """Publish immutable storage until the caller proves no future peer reads."""
        raise NotImplementedError

    async def read(self, tensor: torch.Tensor, source: PayloadRef) -> None:
        """Return only when the received tensor is ready for GPU consumption."""
        raise NotImplementedError

    def require_accepting(self) -> None:
        """Reject new transfers and registration once transport drain starts."""
        if self.draining or self.closed:
            raise RuntimeError("transport is draining or closed")

    async def shutdown(self) -> None:
        """Concrete transports release their engine or sockets after buffers drain."""
        raise NotImplementedError

    async def close(self) -> None:
        """Close only after all publications, transfers and local uses are drained."""
        if self.closed:
            return
        self.draining = True
        for buffer in tuple(self.buffers):
            await buffer.close()
        await self.shutdown()
        self.closed = True
        _live_transports.remove(self)


class TransportUnavailable(RuntimeError):
    """An optional native backend failed initialization before any publication or DMA."""


class MooncakeTransport(TensorTransport):
    """Retain registrations and drain while Mooncake owns native routing and copies."""

    kind = "rdma"
    protocol = "rdma"

    def __init__(
        self, hostname: str, device_name: str = "", device: torch.device | None = None
    ):
        """Initialize the pinned RDMA engine before accepting Worker registrations."""
        from mooncake import engine

        self._submissions: dict[str, asyncio.Lock] = defaultdict(asyncio.Lock)
        self._open_segment = ctypes.CDLL(engine.__file__).openSegmentNoCache
        self._open_segment.argtypes = [ctypes.c_void_p, ctypes.c_char_p]
        self._open_segment.restype = ctypes.c_int32
        self.engine = engine.TransferEngine()
        # The pinned Python protocol argument alone does not select the native
        # backend. Pin the advertised adapter without replacing Mooncake's
        # device/NIC routing or allowing a different backend to be mislabelled.
        overrides = {
            "MC_FORCE_HCA": "1" if self.protocol == "rdma" else None,
            "MC_MACA_HOST_TRANSPORT": "1" if self.protocol == "rdma" else None,
            "MC_FORCE_TCP": None,
            "MC_FORCE_MNNVL": None,
            "MC_INTRANODE_NVLINK": "1" if self.protocol == "nvlink_intra" else None,
            "MC_USE_TENT": None,
            "MC_USE_TEV1": None,
            "USE_BAREX": None,
            "MC_USE_RDMA_TWOSIDED": None,
        }
        saved = {name: os.environ.get(name) for name in overrides}
        try:
            for name, value in overrides.items():
                if value is None:
                    os.environ.pop(name, None)
                else:
                    os.environ[name] = value
            if self.protocol == "rdma":
                topology = json.loads(self.engine.get_local_topology(device_name))
                if not any(any(groups) for groups in topology.values()):
                    raise TransportUnavailable("Mooncake discovered no usable RDMA HCA")
            result = self.engine.initialize(
                hostname, "P2PHANDSHAKE", self.protocol, device_name
            )
            if result == 0 and device is not None:
                # Registration is checked on this Worker's actual GPU before
                # advertising RDMA. Pair reachability is checked at binding.
                probe = torch.empty(65536, dtype=torch.uint8, device=device)
                result = self.engine.register_memory(probe.data_ptr(), probe.nbytes)
                if result == 0:
                    code = self.engine.unregister_memory(probe.data_ptr())
                    if code != 0:
                        raise RuntimeError(f"Mooncake probe unregister failed: {code}")
        except BaseException:
            self.engine = None
            raise
        finally:
            for name, value in saved.items():
                if value is None:
                    os.environ.pop(name, None)
                else:
                    os.environ[name] = value
        if result != 0:
            self.engine = None
            raise TransportUnavailable(
                f"Mooncake {self.protocol} initialization failed: {result}"
            )
        host = f"[{hostname}]" if ":" in hostname else hostname
        self.segment = f"{host}:{self.engine.get_rpc_port()}"
        # Generated by the actual Worker transport, never by its HTTP endpoint.
        # Recreating the engine (including a GPU/NIC change) creates a new identity.
        self.instance_id = uuid4().hex
        super().__init__()

    def register_memory(self, tensor: torch.Tensor) -> None:
        """Register caller-owned storage with the pinned Mooncake engine."""
        result = self.engine.register_memory(tensor.data_ptr(), tensor.nbytes)
        if result != 0:
            raise RuntimeError(f"Mooncake register_memory failed: {result}")

    def unregister(self, tensor: torch.Tensor) -> None:
        """Remove a registration only after TensorBuffer has fenced every use."""
        result = self.engine.unregister_memory(tensor.data_ptr())
        if result != 0:
            raise RuntimeError(f"Mooncake unregister_memory failed: {result}")

    async def publish(self, tensor: torch.Tensor) -> PayloadRef:
        """Describe fenced device storage without copying it through the host."""
        return PayloadRef(
            uuid4().hex,
            self.segment,
            tensor.data_ptr(),
            tensor.nbytes,
            str(tensor.dtype).removeprefix("torch."),
            tuple(tensor.shape),
            self.kind,
        )

    async def read(self, tensor: torch.Tensor, source: PayloadRef) -> None:
        """Submit a device read and poll until the native copy reaches a terminal state."""
        if source.transport != self.kind:
            raise ValueError("publication differs from this Mooncake transport")
        batch_id = await self._submit_read(tensor, source)
        while True:
            # Exactly one entry: transfer_check_status checks task 0 and frees
            # the batch on completion/failure. Never poll a terminal batch twice.
            status = await asyncio.to_thread(
                self.engine.transfer_check_status, batch_id
            )
            if status == 1:
                return
            if status == -1:
                raise RuntimeError(
                    f"Mooncake {self.kind} read failed; buffer is quarantined"
                )
            if status not in (0, -2):
                raise RuntimeError(f"Unexpected Mooncake transfer status: {status}")
            # TIMEOUT does not establish that DMA has stopped. Keep the buffer
            # registered and poll until terminal; role drain may terminate the
            # process if its existing lifecycle deadline expires.
            await asyncio.sleep(0.001)

    async def _submit_read(self, tensor: torch.Tensor, source: PayloadRef) -> int:
        """Refresh registrations and submit per peer; completion polling stays concurrent."""
        # Native engines cache remote registration descriptors. A binding
        # probe can precede model allocations; serialize refresh with submission
        # so concurrent refreshes cannot replace the descriptor before admission.
        async with self._submissions[source.segment]:
            segment = await asyncio.to_thread(
                self._open_segment,
                self.engine.get_engine_ptr(),
                source.segment.encode(),
            )
            if segment < 0:
                raise RuntimeError("Mooncake could not refresh peer registrations")
            batch_id = await asyncio.to_thread(
                self.engine.batch_transfer_async_read,
                source.segment,
                [tensor.data_ptr()],
                [source.address],
                [source.nbytes],
            )
        if batch_id == 0:
            raise RuntimeError(f"Mooncake could not submit the {self.kind} read")
        return batch_id

    async def shutdown(self) -> None:
        """Release the native engine after all registered buffers are gone."""
        self.engine = None


class GpuIpcTransport(MooncakeTransport):
    """Use Mooncake CUDA/MACA IPC copies with the common buffer lifecycle.

    IPC imports peer storage; the runtime owns the physical GPU interconnect.
    """

    kind = "gpu_ipc"

    def __init__(self, hostname: str, device: torch.device):
        """Initialize only granted peers; TensorBuffer retains source allocation ownership."""
        if device.type != "cuda":
            raise TransportUnavailable("GPU IPC requires a CUDA or MACA GPU")
        if getattr(torch.version, "maca", None):
            from .maca import allocation_range, peer_access

            self.protocol = "maca"
        elif torch.version.cuda:
            from .cuda import allocation_range, peer_access

            self.protocol = "nvlink_intra"
        else:
            raise TransportUnavailable("GPU IPC requires a CUDA or MACA runtime")
        self._allocation_range = allocation_range
        self.capability = peer_access(device)
        if self.capability is None:
            raise TransportUnavailable("no granted GPU peer supports P2P")
        self._allocations: dict[int, int] = {}
        super().__init__(hostname)
        # Prove this allocator exports IPC storage before advertising the backend.
        # No publication or peer read exists yet, so initialization can fail closed.
        try:
            probe = torch.empty(65536, dtype=torch.uint8, device=device)
            self.register_memory(probe)
            self.unregister(probe)
        except RuntimeError as error:
            self.engine = None
            self.closed = True
            _live_transports.remove(self)
            raise TransportUnavailable(
                f"GPU IPC registration unavailable: {error}"
            ) from error

    def register_memory(self, tensor: torch.Tensor) -> None:
        """Register each GPU allocation once, retaining every Torch suballocation user."""
        base, size = self._allocation_range(tensor.data_ptr())
        count = self._allocations.get(base, 0)
        if count == 0:
            result = self.engine.register_memory(base, size)
            if result != 0:
                raise RuntimeError(f"Mooncake register_memory failed: {result}")
        self._allocations[base] = count + 1

    def unregister(self, tensor: torch.Tensor) -> None:
        """Unregister an IPC allocation only after its last TensorBuffer is fenced and closed."""
        base, _ = self._allocation_range(tensor.data_ptr())
        count = self._allocations[base]
        if count == 1:
            result = self.engine.unregister_memory(base)
            if result != 0:
                raise RuntimeError(f"Mooncake unregister_memory failed: {result}")
            del self._allocations[base]
        else:
            self._allocations[base] = count - 1
