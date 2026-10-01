# SPDX-License-Identifier: Apache-2.0
# SPDX-FileCopyrightText: Copyright contributors to the Foretoken project

"""Registered tensor ownership for the repository-pinned Mooncake Python API."""

from __future__ import annotations

import asyncio
import math
from dataclasses import dataclass
from typing import Any
from uuid import uuid4

import torch

# Registrations must outlive Python callers, including cancelled coroutines.
# Only explicit, successful close removes this process-owned reference. Uncertain
# DMA completion requires terminating the owning process, never reusing its tensor.
_live_transports: set[MooncakeTransport] = set()

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
    address: int
    nbytes: int
    dtype: str
    shape: tuple[int, ...]

    def __post_init__(self) -> None:
        """Validate dense tensor layout before allocating or submitting a read."""
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
        }

    @classmethod
    def from_wire(cls, value: dict[str, Any]) -> PayloadRef:
        """Decode a descriptor received from a trusted role control connection."""
        publication_id = value["publication_id"]
        segment = value["segment"]
        address = value["address"]
        nbytes = value["nbytes"]
        if not isinstance(publication_id, str) or not publication_id:
            raise ValueError("publication_id must be a nonempty string")
        if not isinstance(segment, str) or not segment:
            raise ValueError("segment must be a nonempty string")
        if type(address) is not int or address <= 0:
            raise ValueError("address must be a positive integer")
        if type(nbytes) is not int or nbytes <= 0:
            raise ValueError("nbytes must be a positive integer")
        dtype = value["dtype"]
        shape = value["shape"]
        if not isinstance(dtype, str) or not isinstance(shape, list):
            raise TypeError("tensor layout requires a dtype name and shape array")
        return cls(publication_id, segment, address, nbytes, dtype, tuple(shape))


class RegisteredTensor:
    """A persistent buffer owned by one transport until explicit unregister."""

    def __init__(self, owner: MooncakeTransport, tensor: torch.Tensor):
        self.owner = owner
        self.tensor = tensor
        self.publication: PayloadRef | None = None
        self.transfer: asyncio.Task[None] | None = None
        self.failed = False
        self.closed = False

    async def publish(
        self, *, ready_event: torch.cuda.Event | None = None
    ) -> PayloadRef:
        """Fence producer writes and lend storage to one reader until its ACK.

        ready_event must already be recorded after every local use of this
        buffer. Without an event, the entire device is synchronized.
        """
        self.owner.require_accepting()
        self._require_idle()
        await self._wait_device(ready_event)
        self.owner.require_accepting()
        self._require_idle()
        self.publication = PayloadRef(
            uuid4().hex,
            self.owner.segment,
            self.tensor.data_ptr(),
            self.tensor.numel() * self.tensor.element_size(),
            str(self.tensor.dtype).removeprefix("torch."),
            tuple(self.tensor.shape),
        )
        return self.publication

    def release_source(self, publication_id: str) -> None:
        """Accept the reader's completed-read acknowledgement, not a timeout."""
        if (
            self.publication is None
            or self.publication.publication_id != publication_id
        ):
            raise ValueError("acknowledgement does not match the live publication")
        self.publication = None

    async def read(
        self, source: PayloadRef, *, ready_event: torch.cuda.Event | None = None
    ) -> None:
        """Pull into this buffer; cancellation keeps DMA and memory ownership alive.

        The caller acknowledges the remote publication only after successful
        completion. A cancelled caller can await this buffer's transfer to obtain
        that result; cancellation never releases the producer automatically.
        ready_event fences previous destination use before the RDMA overwrite;
        omit it to synchronize the device. Await read before launching consumers.
        """
        self.owner.require_accepting()
        self._require_idle()
        nbytes = self.tensor.numel() * self.tensor.element_size()
        if (
            source.nbytes != nbytes
            or _TENSOR_DTYPES[source.dtype] != self.tensor.dtype
            or source.shape != tuple(self.tensor.shape)
        ):
            raise ValueError("source and destination tensor layouts must match")
        self.transfer = asyncio.create_task(self._read(source, ready_event))
        await asyncio.shield(self.transfer)

    async def _read(
        self, source: PayloadRef, ready_event: torch.cuda.Event | None
    ) -> None:
        """Submit one native transfer and poll its terminal status without blocking."""
        # The destination may have been used by the previous GPU computation.
        await self._wait_device(ready_event)
        self.failed = True
        batch_id = await asyncio.to_thread(
            self.owner.engine.batch_transfer_async_read,
            source.segment,
            [self.tensor.data_ptr()],
            [source.address],
            [source.nbytes],
        )
        if batch_id == 0:
            raise RuntimeError("Mooncake could not submit the RDMA read")
        while True:
            # Exactly one entry: transfer_check_status checks task 0 and frees
            # the batch on completion/failure. Never poll a terminal batch twice.
            status = await asyncio.to_thread(
                self.owner.engine.transfer_check_status, batch_id
            )
            if status == 1:
                self.failed = False
                return
            if status == -1:
                raise RuntimeError("Mooncake RDMA read failed; buffer is quarantined")
            if status not in (0, -2):
                raise RuntimeError(f"Unexpected Mooncake transfer status: {status}")
            # TIMEOUT does not establish that DMA has stopped. Keep the buffer
            # registered and poll until terminal; role drain may terminate the
            # process if its existing lifecycle deadline expires.
            await asyncio.sleep(0.001)

    def _require_idle(self) -> None:
        """Reject reuse while exported, transferring, closed, or quarantined."""
        if self.closed or self.failed:
            raise RuntimeError("buffer is closed or quarantined")
        if self.publication is not None:
            raise RuntimeError("buffer is still published to a remote reader")
        if self.transfer is not None and not self.transfer.done():
            raise RuntimeError("buffer has an outstanding transfer")

    async def _wait_device(self, ready_event: torch.cuda.Event | None) -> None:
        """Fence caller-owned GPU work without waiting on unrelated streams.

        The Worker/Runner records the event after joining all streams using this
        storage, and must not re-record it or submit new uses until this operation
        completes. Events do not describe remote DMA completion.
        """
        if ready_event is not None:
            if not self.tensor.is_cuda or ready_event.device != self.tensor.device:
                raise ValueError("ready_event must be recorded on the tensor's device")
            await asyncio.to_thread(ready_event.synchronize)
        elif self.tensor.is_cuda:
            await asyncio.to_thread(torch.cuda.synchronize, self.tensor.device)

    async def close(self, *, ready_event: torch.cuda.Event | None = None) -> None:
        """Unregister after local consumers finish; ready_event fences their uses.

        Without an event, synchronize the device before unregistering. The caller
        must stop submitting work against this buffer before closing it.
        """
        if self.closed:
            return
        self._require_idle()
        await self._wait_device(ready_event)
        self._require_idle()
        result = self.owner.engine.unregister_memory(self.tensor.data_ptr())
        if result != 0:
            self.failed = True
            raise RuntimeError(f"Mooncake unregister_memory failed: {result}")
        self.closed = True
        self.owner.buffers.remove(self)


class MooncakeTransport:
    """Process-local RDMA engine with explicit registration and drain ownership."""

    def __init__(self, hostname: str, device_name: str = ""):
        from mooncake.engine import TransferEngine

        self.engine = TransferEngine()
        result = self.engine.initialize(hostname, "P2PHANDSHAKE", "rdma", device_name)
        if result != 0:
            raise RuntimeError(f"Mooncake RDMA initialization failed: {result}")
        host = f"[{hostname}]" if ":" in hostname else hostname
        self.segment = f"{host}:{self.engine.get_rpc_port()}"
        self.buffers: set[RegisteredTensor] = set()
        self.draining = False
        self.closed = False
        _live_transports.add(self)

    def register(self, tensor: torch.Tensor) -> RegisteredTensor:
        """Retain and register contiguous storage for reuse across DT transfers.

        The caller must not mutate a published tensor or submit GPU work against
        a destination while its read is pending. This class owns registration,
        not the model's allocation or CUDA stream.
        """
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
            other_start = buffer.tensor.data_ptr()
            other_end = (
                other_start + buffer.tensor.numel() * buffer.tensor.element_size()
            )
            if start < other_end and other_start < start + size:
                raise ValueError("tensor overlaps an existing registration")
        result = self.engine.register_memory(start, size)
        if result != 0:
            raise RuntimeError(f"Mooncake register_memory failed: {result}")
        buffer = RegisteredTensor(self, tensor)
        self.buffers.add(buffer)
        return buffer

    def require_accepting(self) -> None:
        """Reject new registration or transfer work once transport teardown starts."""
        if self.draining or self.closed:
            raise RuntimeError("transport is draining or closed")

    async def close(self) -> None:
        """Close only after all publications, transfers, and local uses are drained."""
        if self.closed:
            return
        self.draining = True
        for buffer in tuple(self.buffers):
            await buffer.close()
        self.closed = True
        self.engine = None
        _live_transports.remove(self)
