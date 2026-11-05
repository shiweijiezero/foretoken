# SPDX-License-Identifier: Apache-2.0
# SPDX-FileCopyrightText: Copyright contributors to the Foretoken project

"""Ordered DT control messages with NCCL GPU tensors or a CPU-staged baseline."""

import json
import math
import secrets
import threading
import time
from concurrent.futures import Future
from dataclasses import dataclass
from typing import TYPE_CHECKING, Self

import torch
import zmq

if TYPE_CHECKING:
    from foretoken_dt.nccl import NcclChannel

_DTYPES = {
    "bool": torch.bool,
    "uint8": torch.uint8,
    "int32": torch.int32,
    "int64": torch.int64,
    "float16": torch.float16,
    "bfloat16": torch.bfloat16,
    "float32": torch.float32,
    "float64": torch.float64,
}
TensorLayout = dict[str, tuple[tuple[int, ...], torch.dtype]]


@dataclass
class _TensorHeader:
    """Retain one proposal layout's JSON while batch metadata changes each turn."""

    layout: tuple | None = None
    suffix: bytes = b""


@dataclass
class _ReceiveBuffer:
    """Keep capacity and the current shaped view for one sequential input tensor."""

    storage: torch.Tensor
    tensor: torch.Tensor


@dataclass
class WireMessage:
    """Own received tensors and the dependencies required by their consumers.

    The caller's stream waits for ordinary tensors and a staged request's first
    group. Hidden states and sampler feedback use the explicit events below
    when present. Server proposal buffers are reused by the next receive; enqueue
    all readers on the current stream first. CPU reads still require a device copy.
    """

    metadata: dict
    tensors: dict[str, torch.Tensor]
    # Split proposals may first consume inputs from the pre-sampling group.
    feedback_ready: torch.cuda.Event | None = None
    hidden_ready: torch.cuda.Event | None = None


class RemoteError(RuntimeError):
    """A Draft-side failure reported to the Target's synchronous caller."""


def _encode(
    metadata: dict,
    tensors: dict[str, torch.Tensor],
    use_nccl: bool = False,
    *,
    feedback_layout: TensorLayout | None = None,
    hidden_layout: TensorLayout | None = None,
    cached_header: _TensorHeader | None = None,
) -> tuple[list[bytes], list[torch.Tensor]]:
    """Describe tensors and separate GPU transfers from non-executable CPU frames."""
    layout = None
    if cached_header is not None and use_nccl:
        layout = (
            tuple(
                (name, tensor.shape, tensor.dtype, tensor.is_cuda)
                for name, tensor in tensors.items()
            ),
            tuple(
                (name, tuple(shape), dtype)
                for name, (shape, dtype) in hidden_layout.items()
            )
            if hidden_layout is not None
            else None,
            tuple(
                (name, tuple(shape), dtype)
                for name, (shape, dtype) in feedback_layout.items()
            )
            if feedback_layout is not None
            else None,
        )
        if layout == cached_header.layout:
            frame = (
                b'{"metadata": '
                + json.dumps(metadata, allow_nan=False).encode("utf-8")
                + cached_header.suffix
            )
            return [frame], list(tensors.values())
    descriptors = []
    payloads = []
    gpu_tensors = []
    for name, tensor in tensors.items():
        dtype = str(tensor.dtype).removeprefix("torch.")
        if dtype not in _DTYPES:
            raise ValueError(f"Unsupported DT tensor dtype: {tensor.dtype}")
        gpu = use_nccl and tensor.is_cuda
        if gpu:
            gpu_tensors.append(tensor)
        else:
            # TCP is the explicit comparison path; CUDA tensors then stage on CPU.
            cpu_tensor = tensor.detach().to("cpu").contiguous()
            payloads.append(cpu_tensor.reshape(-1).view(torch.uint8).numpy().tobytes())
        descriptors.append(
            {
                "name": name,
                "dtype": dtype,
                "shape": list(tensor.shape),
                "storage": "nccl" if gpu else "cpu",
            }
        )
    header = {"metadata": metadata, "tensors": descriptors}
    if hidden_layout is not None:
        if not hidden_layout or not feedback_layout or not tensors or payloads:
            raise ValueError("Prepared prefill requires three nonempty NCCL groups")
        header["prefill_splits"] = [
            len(descriptors),
            len(descriptors) + len(hidden_layout),
        ]
        for name, (shape, dtype) in hidden_layout.items():
            wire_dtype = str(dtype).removeprefix("torch.")
            if name in tensors or name in feedback_layout or wire_dtype not in _DTYPES:
                raise ValueError(f"Invalid DT hidden-state descriptor: {name}")
            descriptors.append(
                {"name": name, "shape": shape, "dtype": wire_dtype, "storage": "nccl"}
            )
    if feedback_layout is not None:
        if not tensors or payloads or not feedback_layout:
            raise ValueError("Split DT requests require two nonempty NCCL groups")
        if hidden_layout is None:
            header["split_at"] = len(descriptors)
        for name, (shape, dtype) in feedback_layout.items():
            wire_dtype = str(dtype).removeprefix("torch.")
            if name in tensors or wire_dtype not in _DTYPES:
                raise ValueError(f"Invalid DT feedback descriptor: {name} ({dtype})")
            descriptors.append(
                {"name": name, "shape": shape, "dtype": wire_dtype, "storage": "nccl"}
            )
    if layout is not None and not payloads:
        # Only GPU-only layouts can reuse a header without retaining CPU payloads.
        # Keep ordinary JSON framing so old peers decode the same wire message.
        description = {key: value for key, value in header.items() if key != "metadata"}
        cached_header.layout = layout
        cached_header.suffix = b", " + json.dumps(description, allow_nan=False).encode(
            "utf-8"
        )[1:]
        return [
            b'{"metadata": '
            + json.dumps(metadata, allow_nan=False).encode("utf-8")
            + cached_header.suffix
        ], gpu_tensors
    return [json.dumps(header, allow_nan=False).encode("utf-8"), *payloads], gpu_tensors


def _decode(
    frames: list[bytes],
    nccl: "NcclChannel | None" = None,
    received_tensors: dict[str, torch.Tensor] | None = None,
    *,
    header: dict | None = None,
    receive_buffers: dict[str, _ReceiveBuffer] | None = None,
) -> WireMessage:
    """Decode input or validate an already-posted reply without receiving it twice."""
    if header is None:
        header = json.loads(frames[0])
    if "error" in header:
        raise RemoteError(header["error"])
    metadata = header["metadata"]
    if not isinstance(metadata, dict):
        raise TypeError("DT message metadata must be a JSON object")
    descriptors = header["tensors"]
    if received_tensors is not None and [item["name"] for item in descriptors] != list(
        received_tensors
    ):
        raise ValueError("DT reply does not match its preposted tensor order")
    if sum(item["storage"] == "cpu" for item in descriptors) != len(frames) - 1:
        raise ValueError("DT tensor descriptor and payload counts differ")
    tensors = {}
    gpu_tensors = []
    payloads = iter(frames[1:])
    for descriptor in descriptors:
        name = descriptor["name"]
        shape = descriptor["shape"]
        if not isinstance(name, str) or name in tensors:
            raise ValueError("DT tensor names must be unique strings")
        if not isinstance(shape, list) or any(
            type(dim) is not int or dim < 0 for dim in shape
        ):
            raise ValueError(f"Invalid shape for DT tensor {name}")
        dtype = _DTYPES[descriptor["dtype"]]
        numel = math.prod(shape)
        storage = descriptor["storage"]
        if storage == "nccl":
            if nccl is None:
                raise ValueError("NCCL tensors require the DT transport handshake")
            if received_tensors is not None:
                tensor = received_tensors[name]
                if list(tensor.shape) != shape or tensor.dtype != dtype:
                    raise ValueError(
                        "DT reply does not match its preposted tensor layout"
                    )
            elif receive_buffers is None:
                tensor = torch.empty(shape, dtype=dtype, device=nccl.device)
            else:
                buffer = receive_buffers.get(name)
                if (
                    buffer is None
                    or buffer.storage.dtype != dtype
                    or buffer.storage.numel() < numel
                ):
                    tensor = torch.empty(shape, dtype=dtype, device=nccl.device)
                    buffer = _ReceiveBuffer(tensor, tensor)
                    receive_buffers[name] = buffer
                elif buffer.tensor.shape != tuple(shape):
                    buffer.tensor = buffer.storage.view(-1)[:numel].view(shape)
                tensor = buffer.tensor
            tensors[name] = tensor
            gpu_tensors.append(tensor)
            continue
        if received_tensors is not None:
            raise ValueError("A preposted DT reply must contain only NCCL tensors")
        if storage != "cpu":
            raise ValueError(f"Unknown DT tensor storage: {storage}")
        if receive_buffers is not None:
            receive_buffers.pop(name, None)
        payload = next(payloads)
        expected_bytes = numel * torch.empty((), dtype=dtype).element_size()
        if len(payload) != expected_bytes:
            raise ValueError(
                f"DT tensor {name} has {len(payload)} bytes, expected {expected_bytes}"
            )
        if numel:
            # frombuffer retains the writable bytearray, so tensors remain valid
            # after the ZMQ frames and this message header have been released.
            tensor = torch.frombuffer(bytearray(payload), dtype=dtype).reshape(shape)
        else:
            tensor = torch.empty(shape, dtype=dtype)
        tensors[name] = tensor
    if receive_buffers is not None and len(receive_buffers) > len(gpu_tensors):
        for name in receive_buffers.keys() - tensors.keys():
            del receive_buffers[name]
    feedback_ready = None
    hidden_ready = None
    if "prefill_splits" in header:
        splits = header["prefill_splits"]
        if (
            nccl is None
            or received_tensors is not None
            or not isinstance(splits, list)
            or len(splits) != 2
            or any(type(split) is not int for split in splits)
            or not 0 < splits[0] < splits[1] < len(gpu_tensors)
            or len(gpu_tensors) != len(tensors)
            or set(list(tensors)[splits[1] :])
            != {"last_sampled", "num_sampled", "num_rejected"}
        ):
            raise ValueError("Invalid DT prefill group layout")
        known_end, hidden_end = splits
        hidden_ready, feedback_ready = nccl.receive_prefill(
            gpu_tensors[:known_end],
            gpu_tensors[known_end:hidden_end],
            gpu_tensors[hidden_end:],
        )
    elif "split_at" in header:
        split = header["split_at"]
        if (
            nccl is None
            or received_tensors is not None
            or type(split) is not int
            or not 0 < split < len(gpu_tensors)
            or len(gpu_tensors) != len(tensors)
        ):
            raise ValueError("Invalid DT split request layout")
        deferred = metadata.get("operation") == "propose" and set(
            list(tensors)[split:]
        ) == {
            "last_sampled",
            "num_sampled",
            "num_rejected",
        }
        done = nccl.receive_groups(
            gpu_tensors[:split], gpu_tensors[split:], defer_second=deferred
        )
        if deferred:
            feedback_ready = done
    elif nccl is not None and received_tensors is None:
        nccl.receive(gpu_tensors)
    return WireMessage(metadata, tensors, feedback_ready, hidden_ready)


class _Peer:
    """Own the socket and context for one single-threaded DT connection."""

    def __init__(self, endpoint: str, kind: int, timeout_ms: int | None):
        self._nccl: NcclChannel | None = None
        self._timeout_ms = timeout_ms
        self._endpoint = endpoint
        self._server = kind == zmq.ROUTER
        self._tensor_header = _TensorHeader()
        self._route: bytes | None = None
        self._context = zmq.Context()
        self._socket = self._context.socket(kind)
        self._socket.setsockopt(zmq.LINGER, 0)
        if timeout_ms is not None:
            self._socket.setsockopt(zmq.SNDTIMEO, timeout_ms)
            self._socket.setsockopt(zmq.RCVTIMEO, timeout_ms)
        try:
            if self._server:
                self._socket.bind(endpoint)
            else:
                self._socket.connect(endpoint)
        except zmq.ZMQError:
            self.close()
            raise

    def close(self) -> None:
        """Release this peer's resources, respecting its bounded shutdown drain."""
        if self._nccl is not None:
            self._nccl.close()
        self._socket.close()
        self._context.term()

    def _send(self, metadata: dict, tensors: dict[str, torch.Tensor] | None) -> None:
        """Publish the tensor description before posting matching NCCL sends."""
        if self._nccl is not None:
            self._nccl.check_error()
        frames, gpu_tensors = _encode(
            metadata,
            tensors if tensors is not None else {},
            self._nccl is not None,
            cached_header=self._tensor_header
            if metadata.get("operation") in ("propose", "prefill")
            else None,
        )
        self._send_frames(frames)
        if self._nccl is not None:
            self._nccl.send(gpu_tensors)

    def _send_frames(self, frames: list[bytes]) -> None:
        """Keep REQ framing and route each normal response back to its requester."""
        envelope = [self._route, b""] if self._server else [b""]
        self._socket.send_multipart(envelope + frames)

    def _read_frames(self) -> list[bytes]:
        """Strip the routing envelope before decoding the native control message."""
        frames = self._socket.recv_multipart()
        if self._server:
            self._route = frames.pop(0)
        if len(frames) < 2 or frames[0] != b"":
            raise ValueError("Malformed DT control envelope")
        return frames[1:]

    def _receive_frames(self) -> list[bytes]:
        """Wait for control frames while surfacing asynchronous GPU failures."""
        if self._nccl is None:
            return self._read_frames()
        deadline = (
            time.monotonic() + self._timeout_ms / 1000
            if self._timeout_ms is not None
            else None
        )
        while True:
            self._nccl.check_error()
            wait_ms = 100
            if deadline is not None:
                remaining_ms = math.ceil((deadline - time.monotonic()) * 1000)
                if remaining_ms <= 0:
                    raise zmq.Again()
                wait_ms = min(wait_ms, remaining_ms)
            if self._socket.poll(wait_ms, zmq.POLLIN):
                self._nccl.check_error()
                return self._read_frames()

    def __enter__(self) -> Self:
        return self

    def __exit__(self, exc_type, exc_value, traceback) -> None:
        self.close()


class Client(_Peer):
    """Target-owned request socket; a transport failure terminates the session."""

    def __init__(self, endpoint: str, timeout_ms: int):
        """Connect to Draft with a timeout covering each send or receive wait."""
        if timeout_ms <= 0:
            raise ValueError("DT timeout must be positive")
        self._closing = False
        self._error_listener: threading.Thread | None = None
        self.gpu_only_replies = False
        self.prefill_without_reply = False
        self.prefill_prepare = False
        self.free_without_reply = False
        super().__init__(endpoint, zmq.DEALER, timeout_ms)
        self._pending_reply: dict[str, torch.Tensor] | None = None
        self._split_feedback: tuple[TensorLayout, bool] | None = None
        self._split_requests = False
        self._prefill_features: TensorLayout | None = None
        self._prefill_feedback: tuple[TensorLayout, bool] | None = None

    def close(self) -> None:
        """Abort pending transfers before releasing this client's reply buffers."""
        self._closing = True
        try:
            super().close()
        finally:
            if self._error_listener is not None:
                self._error_listener.join()
            self._pending_reply = None
            self._split_feedback = None
            self._prefill_features = None
            self._prefill_feedback = None

    def _listen_for_error(self, token: str) -> None:
        """Register one blocking error receiver; it owns its socket for its lifetime."""
        assert self._nccl is not None and self._timeout_ms is not None
        registered = Future()
        channel = self._nccl

        def listen() -> None:
            try:
                with self._context.socket(zmq.DEALER) as socket:
                    socket.setsockopt(zmq.LINGER, 1000)
                    socket.setsockopt(zmq.SNDTIMEO, self._timeout_ms)
                    socket.connect(self._endpoint)
                    socket.send_multipart(
                        [b"", json.dumps({"watch_error": token}).encode()]
                    )
                    if not socket.poll(self._timeout_ms, zmq.POLLIN):
                        raise TimeoutError("Draft did not register its error receiver")
                    if socket.recv_multipart() != [b"", b"ready"]:
                        raise ValueError("Invalid DT error channel registration")
                    registered.set_result(None)
                    # No successful decode traffic ever wakes this thread. Context
                    # termination interrupts recv during normal client shutdown.
                    envelope, frame = socket.recv_multipart()
                    if envelope != b"":
                        raise ValueError("Invalid DT error channel envelope")
                    channel.fail(RemoteError(json.loads(frame)["error"]))
                    socket.send_multipart([b"", b"error_seen"])
            except Exception as error:
                if not registered.done():
                    registered.set_exception(error)
                elif not self._closing:
                    channel.fail(error)

        self._error_listener = threading.Thread(
            target=listen, name="dt-draft-error", daemon=True
        )
        self._error_listener.start()
        registered.result()
        self.gpu_only_replies = True

    def check_error(self) -> None:
        """Check in-memory terminal state before publishing candidate-dependent output."""
        if self._nccl is not None:
            self._nccl.check_error()

    def enable_nccl(
        self, device: torch.device, *, split_requests: bool = False
    ) -> None:
        """Rendezvous with Draft over ZMQ before sending vocabulary weights on GPU."""
        from foretoken_dt.nccl import NcclChannel

        if self._nccl is not None:
            raise RuntimeError("DT NCCL transport is already initialized")
        assert self._timeout_ms is not None
        try:
            self._nccl = NcclChannel(device, self._timeout_ms)
            unique_id = self._nccl.unique_id()
            token = secrets.token_hex(16)
            setup = {
                "unique_id": unique_id,
                "timeout_ms": self._timeout_ms,
                "gpu_only_replies": token,
            }
            if split_requests:
                setup["split_requests"] = True
            self._send_frames([json.dumps({"nccl_init": setup}).encode()])
            response = _decode(self._receive_frames())
            if response.metadata.get("status") != "nccl_ready":
                raise ValueError("Unexpected DT NCCL handshake response")
            # Server acknowledges before joining, so neither rank waits for an
            # application reply while its peer is inside ncclCommInitRank.
            self._nccl.connect(unique_id, rank=0)
            # Establish Draft's send path during startup. A first proposal may
            # report an error without a GPU payload; its preposted receive must
            # not be the operation that lazily connects this direction.
            self._nccl.receive([torch.empty(1, dtype=torch.uint8, device=device)])
            # An older peer enters ncclCommInitRank immediately after its reply.
            # Complete bootstrap before rejecting an unsupported split protocol.
            if split_requests and response.metadata.get("split_requests") is not True:
                raise ValueError("Draft does not support split NCCL requests")
            self._split_requests = split_requests
            if response.metadata.get("gpu_only_replies") is True:
                self._listen_for_error(token)
                self.prefill_without_reply = (
                    response.metadata.get("prefill_without_reply") is True
                )
                self.prefill_prepare = response.metadata.get("prefill_prepare") is True
                self.free_without_reply = (
                    response.metadata.get("free_without_reply") is True
                )
        except zmq.Again as error:
            self.close()
            raise TimeoutError("Timed out establishing the DT NCCL session") from error
        except Exception:
            self.close()
            raise

    def request(
        self, metadata: dict, tensors: dict[str, torch.Tensor] | None = None
    ) -> WireMessage:
        """Return Draft tensors ordered on the current CUDA stream, or raise its error."""
        self.send_request(metadata, tensors)
        response = self.receive_pending()
        assert response is not None
        return response

    def send_request(
        self,
        metadata: dict,
        tensors: dict[str, torch.Tensor] | None = None,
        *,
        reply_tensors: dict[str, torch.Tensor] | None = None,
    ) -> None:
        """Send one request and optionally prepost its fixed-layout NCCL reply.

        The producer enforces the reply layout before sending candidates.
        The caller can queue consumers immediately. Requests with a control
        reply must finish that reply before sending more work.
        """
        if self._pending_reply is not None:
            raise RuntimeError("Receive the pending DT reply before sending more work")
        gpu_only = self._uses_gpu_only_reply(metadata)
        prefill = metadata.get("operation") == "prefill"
        free = metadata.get("free_without_reply") is True
        if prefill and (not self.prefill_without_reply or reply_tensors is not None):
            raise ValueError(
                "Unfinished DT prefill requires negotiated one-way requests"
            )
        if free and (
            not self.free_without_reply
            or metadata.get("operation") != "free"
            or tensors
            or reply_tensors is not None
        ):
            raise ValueError("One-way DT release requires negotiated control-only free")
        if reply_tensors is not None or gpu_only:
            self._check_reply_buffers(reply_tensors, gpu_only)
        try:
            self._send(metadata, tensors)
            if not prefill and not free:
                self._post_reply(reply_tensors, gpu_only)
        except zmq.Again as error:
            self.close()
            raise TimeoutError(
                "Timed out sending the DT request; session closed"
            ) from error
        except Exception:
            self.close()
            raise

    def begin_request(
        self,
        metadata: dict,
        features: dict[str, torch.Tensor],
        feedback_layout: TensorLayout,
    ) -> None:
        """Publish both groups and send features while the caller proceeds to sample."""
        if self._pending_reply is not None:
            raise RuntimeError("Receive the pending DT reply before sending more work")
        if self._nccl is None or not self._split_requests:
            raise RuntimeError("Split DT requests require a negotiated NCCL session")
        gpu_only = self._uses_gpu_only_reply(metadata)
        try:
            self._nccl.check_error()
            frames, gpu_tensors = _encode(
                metadata,
                features,
                use_nccl=True,
                feedback_layout=feedback_layout,
                cached_header=self._tensor_header,
            )
            self._send_frames(frames)
            self._nccl.enqueue_send(gpu_tensors)
            self._split_feedback = (feedback_layout, gpu_only)
            self._pending_reply = {}
        except zmq.Again as error:
            self.close()
            raise TimeoutError(
                "Timed out sending the DT request; session closed"
            ) from error
        except Exception:
            self.close()
            raise

    def finish_request(
        self,
        tensors: dict[str, torch.Tensor],
        *,
        reply_tensors: dict[str, torch.Tensor],
    ) -> None:
        """Send real sampler feedback, fence source reuse, then prepost the candidates."""
        if self._split_feedback is None or self._nccl is None:
            raise RuntimeError("No split DT request awaits sampler feedback")
        layout, gpu_only = self._split_feedback
        try:
            self._check_reply_buffers(reply_tensors, gpu_only)
            if tensors.keys() != layout.keys():
                raise ValueError("DT sampler feedback differs from its declared names")
            feedback = []
            for name, (shape, dtype) in layout.items():
                tensor = tensors[name]
                if (
                    tensor.shape != shape
                    or tensor.dtype != dtype
                    or tensor.device != self._nccl.device
                ):
                    raise ValueError(f"DT sampler feedback {name} changed layout")
                feedback.append(tensor)
            # The early send and feedback share one NCCL stream. Fencing this
            # send protects graph feature storage before the next forward writes
            # it, without inserting a dependency before this round's sampling.
            self._nccl.send(feedback)
            self._split_feedback = None
            self._post_reply(reply_tensors, gpu_only)
        except Exception:
            self.close()
            raise

    def begin_prefill(
        self,
        metadata: dict,
        known: dict[str, torch.Tensor],
        hidden_layout: TensorLayout,
        feedback_layout: TensorLayout,
    ) -> None:
        """Post one prefill transaction and its known inputs before Target's forward."""
        if self._pending_reply is not None:
            raise RuntimeError("Receive the pending DT reply before preparing prefill")
        if self._nccl is None or not self.prefill_prepare:
            raise RuntimeError("Prepared prefill requires a negotiated NCCL session")
        if metadata.get("operation") not in ("prefill", "propose"):
            raise ValueError("Prepared prefill must execute a native proposal")
        try:
            frames, tensors = _encode(
                metadata,
                known,
                use_nccl=True,
                hidden_layout=hidden_layout,
                feedback_layout=feedback_layout,
                cached_header=self._tensor_header,
            )
            self._send_frames(frames)
            self._nccl.enqueue_send_async(tensors)
            self._prefill_features = hidden_layout
            self._prefill_feedback = (
                feedback_layout,
                metadata["operation"] == "propose",
            )
            self._pending_reply = {}
        except Exception:
            self.close()
            raise

    def send_prefill_features(self, tensors: dict[str, torch.Tensor]) -> None:
        """Send the prepared prefill's hidden states while Target proceeds to sample."""
        if self._prefill_features is None or self._nccl is None:
            raise RuntimeError("No prepared DT prefill awaits hidden states")
        try:
            values = self._prefill_group(tensors, self._prefill_features)
            self._nccl.enqueue_send(values)
            self._prefill_features = None
        except Exception:
            self.close()
            raise

    def finish_prefill(
        self,
        tensors: dict[str, torch.Tensor],
        reply_tensors: dict[str, torch.Tensor] | None,
    ) -> None:
        """Send sampling feedback, protect source reuse and receive final-chunk candidates."""
        if (
            self._prefill_feedback is None
            or self._prefill_features is not None
            or self._nccl is None
        ):
            raise RuntimeError("Prepared DT prefill is not ready for feedback")
        layout, has_reply = self._prefill_feedback
        try:
            if has_reply:
                self._check_reply_buffers(reply_tensors, False)
            elif reply_tensors is not None:
                raise ValueError("An unfinished prefill cannot receive candidates")
            values = self._prefill_group(tensors, layout)
            # All three groups share the stream. This fence protects the known
            # inputs and hidden-state buffers before the next forward reuses them.
            self._nccl.send(values)
            self._prefill_feedback = None
            self._pending_reply = None
            if has_reply:
                self._post_reply(reply_tensors, False)
        except Exception:
            self.close()
            raise

    def _prefill_group(
        self,
        tensors: dict[str, torch.Tensor],
        layout: TensorLayout,
    ) -> list[torch.Tensor]:
        """Check a predeclared prefill group before sending bytes into posted receives."""
        assert self._nccl is not None
        if tensors.keys() != layout.keys():
            raise ValueError("Prepared DT prefill tensor names changed")
        values = []
        for name, (shape, dtype) in layout.items():
            tensor = tensors[name]
            if (
                tensor.shape != shape
                or tensor.dtype != dtype
                or tensor.device != self._nccl.device
            ):
                raise ValueError(f"Prepared DT prefill tensor {name} changed layout")
            values.append(tensor)
        return values

    def _uses_gpu_only_reply(self, metadata: dict) -> bool:
        """Select the negotiated success path before publishing the request header."""
        gpu_only = metadata.get("gpu_only_reply") is True
        if gpu_only and (
            not self.gpu_only_replies or metadata.get("operation") != "propose"
        ):
            raise ValueError("GPU-only DT replies require a negotiated proposal")
        return gpu_only

    def _check_reply_buffers(
        self, reply_tensors: dict[str, torch.Tensor] | None, gpu_only: bool
    ) -> None:
        """Check fixed reply buffers before submitting their request."""
        if self._nccl is None or not reply_tensors:
            raise ValueError("Preposted DT replies require NCCL and CUDA buffers")
        if gpu_only and list(reply_tensors) != ["draft_tokens"]:
            raise ValueError("GPU-only DT proposals require a candidate buffer")
        if any(
            tensor.device != self._nccl.device or not tensor.is_contiguous()
            for tensor in reply_tensors.values()
        ):
            raise ValueError(
                "Preposted DT reply buffers must be contiguous on this GPU"
            )

    def _post_reply(
        self, reply_tensors: dict[str, torch.Tensor] | None, gpu_only: bool
    ) -> None:
        """Post candidate receives and retain buffers only for an expected control reply."""
        self._pending_reply = None if gpu_only else (reply_tensors or {})
        if reply_tensors is not None:
            assert self._nccl is not None
            self._nccl.receive(list(reply_tensors.values()))

    def receive_pending(self) -> WireMessage | None:
        """Finish an expected control reply, or return None when none is pending."""
        if self._pending_reply is None:
            return None
        try:
            if self._split_feedback is not None or self._prefill_feedback is not None:
                raise RuntimeError("Split DT request abandoned before sampler feedback")
            response = self._receive_frames()
            result = _decode(response, self._nccl, self._pending_reply or None)
            self._pending_reply = None
            return result
        except zmq.Again as error:
            # Retrying a stateful proposal could execute it twice. Abort instead.
            self.close()
            raise TimeoutError(
                "Timed out waiting for the DT peer; the session is closed"
            ) from error
        except Exception:
            # A header or GPU transfer may already have been consumed. Never
            # continue a stateful session after a remote or protocol failure.
            self.close()
            raise


class Server(_Peer):
    """Draft-owned reply socket, used sequentially by the Draft service loop."""

    def __init__(
        self,
        endpoint: str,
        timeout_ms: int | None = None,
        device: str | torch.device | None = None,
    ):
        """Bind the Draft endpoint; None waits indefinitely, including while idle."""
        self._device = torch.device(device) if device is not None else None
        self._owner: bytes | None = None
        self._error_token: str | None = None
        self._error_route: bytes | None = None
        self._reply_mode = "control"
        self._input_buffers: dict[str, _ReceiveBuffer] = {}
        super().__init__(endpoint, zmq.ROUTER, timeout_ms)
        # Error replies must have time to leave the socket when the service exits
        # immediately afterward. Bound cleanup if the Target has disappeared.
        self._socket.setsockopt(zmq.LINGER, 1000)

    def receive(self) -> WireMessage:
        """Return ordered input; queue readers on this stream before the next receive."""
        while True:
            try:
                frames = self._receive_frames()
            except ValueError as error:
                if self._owner is not None and self._route != self._owner:
                    self._send_frames([b'{"error":"Malformed DT control envelope"}'])
                    continue
                self.reply_error(error)
                raise
            except (RuntimeError, TimeoutError) as error:
                self._notify_error(error)
                raise
            self._reply_mode = "control"
            try:
                header = json.loads(frames[0])
                if not isinstance(header, dict):
                    raise ValueError("DT control header must be an object")
            except (ValueError, UnicodeError) as error:
                if self._owner is None or self._owner == self._route:
                    self.reply_error(error)
                    raise
                self._send_frames([b'{"error":"Unexpected DT peer"}'])
                continue
            if "watch_error" in header:
                if (
                    self._error_token is None
                    or header["watch_error"] != self._error_token
                    or self._error_route is not None
                    or self._route == self._owner
                ):
                    self._send_frames([b'{"error":"Unexpected error receiver"}'])
                    continue
                self._error_route = self._route
                self._send_frames([b"ready"])
                continue
            if self._owner is None:
                self._owner = self._route
            elif self._route != self._owner:
                self._send_frames([b'{"error":"Draft is owned by another Target"}'])
                continue
            if "nccl_init" not in header:
                try:
                    meta = header["metadata"]
                    if "prefill_splits" in header and self._error_route is None:
                        raise ValueError(
                            "Prepared DT prefill requires the error channel"
                        )
                    if meta.get("operation") == "prefill":
                        if self._error_route is None:
                            raise ValueError(
                                "One-way DT prefill requires the error channel"
                            )
                        self._reply_mode = "none"
                    elif meta.get("free_without_reply") is True:
                        if (
                            self._error_route is None
                            or meta.get("operation") != "free"
                            or header["tensors"]
                        ):
                            raise ValueError("One-way DT release was not negotiated")
                        self._reply_mode = "none"
                    elif meta.get("gpu_only_reply") is True:
                        if (
                            self._error_route is None
                            or meta.get("operation") != "propose"
                        ):
                            raise ValueError("GPU-only DT proposal was not negotiated")
                        self._reply_mode = "gpu"
                    return _decode(
                        frames,
                        self._nccl,
                        header=header,
                        receive_buffers=self._input_buffers
                        if self._nccl is not None
                        and meta.get("operation") in ("propose", "prefill")
                        else None,
                    )
                except Exception as error:
                    self.reply_error(error)
                    raise
            from foretoken_dt.nccl import NcclChannel

            try:
                if self._device is None or self._nccl is not None:
                    raise ValueError("Draft cannot initialize a new NCCL session")
                setup = header["nccl_init"]
                self._nccl = NcclChannel(self._device, setup["timeout_ms"])
            except Exception as error:
                self.reply_error(error)
                raise
            response = {"status": "nccl_ready"}
            if isinstance(setup.get("gpu_only_replies"), str):
                self._error_token = setup["gpu_only_replies"]
                response["gpu_only_replies"] = True
                response["prefill_without_reply"] = True
                response["prefill_prepare"] = True
                response["free_without_reply"] = True
            if setup.get("split_requests") is True:
                response["split_requests"] = True
            self.reply(response)
            self._nccl.connect(setup["unique_id"], rank=1)
            self._nccl.send(
                [torch.zeros(1, dtype=torch.uint8, device=self._nccl.device)]
            )

    def close(self) -> None:
        """Retire communication before dropping retained proposal receive storage."""
        try:
            super().close()
        finally:
            self._input_buffers.clear()

    def reply(
        self, metadata: dict, tensors: dict[str, torch.Tensor] | None = None
    ) -> None:
        """Complete the outstanding request with Draft's metadata and tensors."""
        try:
            if self._reply_mode == "none":
                return
            if self._reply_mode == "gpu":
                assert self._nccl is not None and tensors is not None
                self._nccl.send([tensors["draft_tokens"]])
                return
            self._send(metadata, tensors)
        except Exception as error:
            # A successful control frame may have already gone out. The separate
            # error receiver can still interrupt a stalled Target CUDA consumer.
            self._notify_error(error)
            raise

    def _notify_error(self, error: Exception) -> bytes:
        """Give Target a chance to latch failure before Draft tears down NCCL."""
        frame = json.dumps({"error": f"{type(error).__name__}: {error}"}).encode()
        if self._error_route is not None:
            self._socket.send_multipart([self._error_route, b"", frame])
            # A peer-side abort could unblock a pending receive. Target first
            # latches the error seen by its output publisher, then acknowledges.
            # Only a terminal session takes this path. Discard queued work and
            # bound teardown if Target has already stopped or disconnected.
            deadline = time.monotonic() + 1.0
            while (remaining := deadline - time.monotonic()) > 0:
                if not self._socket.poll(math.ceil(remaining * 1000), zmq.POLLIN):
                    break
                if self._socket.recv_multipart() == [self._error_route, b"", b"error_seen"]:
                    break
        return frame

    def reply_error(self, error: Exception) -> None:
        """Report a handled service failure so Target does not wait until timeout."""
        frame = self._notify_error(error)
        if self._reply_mode == "control":
            self._send_frames([frame])
