# SPDX-License-Identifier: Apache-2.0
# SPDX-FileCopyrightText: Copyright contributors to the Foretoken project

"""Worker-owned proposal distributions and asynchronous Mooncake transfers."""

from __future__ import annotations

import asyncio
from concurrent.futures import Future
from dataclasses import dataclass, field
from threading import Thread
from typing import Any

import torch

from foretoken_dt.transport import MooncakeTransport, PayloadRef, RegisteredTensor


@dataclass
class _Artifact:
    """Keep device storage alive through computation, transfer and acknowledgement."""

    rows: list[torch.Tensor] = field(default_factory=list)
    event: torch.cuda.Event | None = None
    buffer: RegisteredTensor | None = None
    operation: Future[Any] | None = None
    release: Future[Any] | None = None
    source: PayloadRef | None = None
    acknowledgement: str | None = None


class _WorkerIO:
    """Adapt MRV2 hooks to device distributions owned by one role Worker."""

    def __init__(
        self,
        role: str,
        hostname: str,
        nic: str,
        device: torch.device,
        vocab_size: int,
        num_speculative_steps: int,
    ):
        self.capture_proposals = role == "draft"
        self.device = device
        self.vocab_size = vocab_size
        self.num_speculative_steps = num_speculative_steps
        self.close_operation: Future[Any] | None = None
        self.transport = MooncakeTransport(hostname, nic)
        self.artifacts: dict[str, _Artifact] = {}
        # Remote peers cache registration keys. Keep addresses registered across
        # rounds instead of freeing and re-registering recycled CUDA allocations.
        # This background-loop-owned pool retains peak concurrency per shape.
        self.idle_buffers: dict[tuple[int, ...], list[RegisteredTensor]] = {}
        self.requests: dict[str, tuple[str, bool]] = {}
        self.staged: dict[str, tuple[str, int]] = {}
        self.loop = asyncio.new_event_loop()
        self.thread = Thread(target=self.loop.run_forever, daemon=True)
        self.thread.start()

    def add_request(self, req_id: str, sampling_params: Any) -> None:
        """Bind a Draft engine request to its role-owned round artifact."""
        if not self.capture_proposals:
            return
        artifact_id = (sampling_params.extra_args or {}).get("dt_artifact_id")
        if artifact_id is None:
            return
        # Resumed requests retain already sampled rows; prefill chunks are skipped.
        self.artifacts.setdefault(artifact_id, _Artifact())
        self.requests[req_id] = (artifact_id, sampling_params.temperature == 0)

    def remove_request(self, req_id: str) -> None:
        """Forget engine bindings without revoking a live RDMA publication."""
        self.requests.pop(req_id, None)
        self.staged.pop(req_id, None)

    def on_sample(self, input_batch: Any, sampler_output: Any) -> None:
        """Capture actual post-processing distributions on the compute stream."""
        if not self.capture_proposals:
            return
        logits = sampler_output.processed_logits
        if logits is None:
            raise RuntimeError("Draft sampling did not export processed logits")
        for index, req_id in enumerate(input_batch.req_ids):
            binding = self.requests.get(req_id)
            if binding is None:
                continue
            if (
                input_batch.num_computed_tokens_np[index]
                + input_batch.num_scheduled_tokens[index]
                < input_batch.prefill_len_np[index]
            ):
                continue
            artifact_id, greedy = binding
            artifact = self.artifacts[artifact_id]
            row_index = int(input_batch.cu_num_logits_np[index])
            if greedy:
                row = torch.full_like(
                    logits[row_index], -torch.inf, dtype=torch.float32
                )
                token = sampler_output.sampled_token_ids[index].reshape(-1)[:1]
                row.scatter_(0, token.to(torch.int64), 0.0)
            else:
                row = torch.log_softmax(logits[row_index].float(), dim=-1)
            artifact.rows.append(row)
            artifact.event = torch.cuda.Event()
            artifact.event.record(torch.cuda.current_stream(self.device))

    def draft_logits(
        self, input_batch: Any, temperatures_gpu: torch.Tensor
    ) -> torch.Tensor:
        """Map ready q rows to current persistent slots for native rejection sampling."""
        active: list[tuple[int, _Artifact, int]] = []
        for index, req_id in enumerate(input_batch.req_ids):
            count = int(
                input_batch.cu_num_logits_np[index + 1]
                - input_batch.cu_num_logits_np[index]
                - 1
            )
            if count <= 0:
                continue
            artifact_id, generation = self.staged[req_id]
            if input_batch.external_draft_generations[req_id] != generation:
                raise ValueError(
                    "staged distribution belongs to a different generation"
                )
            artifact = self.artifacts[artifact_id]
            if artifact.operation is None or not artifact.operation.done():
                raise RuntimeError("Target scheduled an unfinished proposal transfer")
            artifact.operation.result()
            if artifact.buffer is None or artifact.buffer.tensor.shape[0] < count:
                raise ValueError(
                    "proposal distribution rows do not match candidate count"
                )
            active.append((int(input_batch.idx_mapping_np[index]), artifact, count))
        # The native statistics kernel visits every scheduled slot and every
        # local position below configured K, including shortened-chain bonus
        # rows and zero-candidate requests in a mixed batch. Those unused q
        # rows need allocated, finite storage even though acceptance skips them.
        result = torch.zeros(
            (
                int(input_batch.idx_mapping_np.max()) + 1,
                self.num_speculative_steps,
                self.vocab_size,
            ),
            dtype=torch.float32,
            device=self.device,
        )
        for slot, artifact, count in active:
            if artifact.buffer.tensor.shape[1] != self.vocab_size:
                raise ValueError("proposal vocabulary sizes differ within a batch")
            # The rejection kernel divides draft logits by Target temperature.
            # q was already normalized using Draft sampling parameters.
            scale = torch.where(temperatures_gpu[slot] > 0, temperatures_gpu[slot], 1.0)
            result[slot, :count].copy_(artifact.buffer.tensor[:count] * scale)
            artifact.event = torch.cuda.Event()
            artifact.event.record(torch.cuda.current_stream(self.device))
        return result

    def _acquire_buffer(self, shape: tuple[int, ...]) -> RegisteredTensor:
        """Borrow a registered matrix on the transport loop, retaining its RDMA key."""
        idle = self.idle_buffers.setdefault(shape, [])
        if idle:
            return idle.pop()
        tensor = torch.empty(shape, dtype=torch.float32, device=self.device)
        return self.transport.register(tensor)

    async def _publish(self, artifact: _Artifact) -> dict[str, Any]:
        """Fence captured rows and fill a reusable publication buffer for Target."""
        with torch.cuda.device(self.device):
            if artifact.event is not None:
                await asyncio.to_thread(artifact.event.synchronize)
            artifact.buffer = self._acquire_buffer(
                (len(artifact.rows), self.vocab_size)
            )
            torch.stack(artifact.rows, out=artifact.buffer.tensor)
            event = torch.cuda.Event()
            event.record(torch.cuda.current_stream(self.device))
            payload = await artifact.buffer.publish(ready_event=event)
            artifact.rows.clear()
            return payload.to_wire()

    async def _receive(self, artifact: _Artifact) -> None:
        """Borrow local registered storage and await RDMA outside execution."""
        source = artifact.source
        with torch.cuda.device(self.device):
            artifact.buffer = self._acquire_buffer(source.shape)
            event = torch.cuda.Event()
            event.record(torch.cuda.current_stream(self.device))
            await artifact.buffer.read(source, ready_event=event)

    async def _release(self, artifact_id: str, artifact: _Artifact) -> None:
        """Wait for DMA and local consumers; retain exports without a reader ACK."""
        if artifact.operation is not None:
            await asyncio.wrap_future(artifact.operation)
        buffer = artifact.buffer
        if buffer is not None:
            if buffer.publication is not None:
                if artifact.acknowledgement is None:
                    return
                buffer.release_source(artifact.acknowledgement)
            if artifact.event is not None:
                await asyncio.to_thread(artifact.event.synchronize)
            # The operation future proves DMA completion; this event fences the
            # last local GPU consumer. Failed reads never reach the idle pool.
            artifact.buffer = None
            self.idle_buffers[tuple(buffer.tensor.shape)].append(buffer)
        self.artifacts.pop(artifact_id, None)


class DraftTargetWorkerExtension:
    """vLLM worker extension used by the internal role service's collective RPC."""

    def dt_initialize(self, role: str, hostname: str, nic: str = "") -> None:
        """Install one process-owned Connector after vLLM creates its Runner."""
        if role not in ("draft", "target"):
            raise ValueError("DT role must be draft or target")
        self.dt_io = _WorkerIO(
            role,
            hostname,
            nic,
            self.device,
            self.model_runner.vocab_size,
            self.model_runner.num_speculative_steps,
        )
        self.model_runner.set_external_speculation_io(self.dt_io)

    def dt_publish(self, artifact_id: str) -> dict[str, Any]:
        """Begin publication or return its ready descriptor without remote waits."""
        io = self.dt_io
        artifact = io.artifacts[artifact_id]
        if artifact.operation is None:
            if not artifact.rows:
                raise ValueError("Draft artifact has no sampled distributions")
            artifact.operation = asyncio.run_coroutine_threadsafe(
                io._publish(artifact), io.loop
            )
        if not artifact.operation.done():
            return {"ready": False}
        return {"ready": True, "payload": artifact.operation.result()}

    def dt_receive(self, artifact_id: str, payload: dict[str, Any]) -> dict[str, bool]:
        """Start or poll one immutable publication's RDMA read on this Worker."""
        io = self.dt_io
        source = PayloadRef.from_wire(payload)
        if source.dtype != "float32" or len(source.shape) != 2:
            raise ValueError(
                "proposal distribution requires float32 [tokens, vocabulary]"
            )
        if source.shape[1] != io.vocab_size:
            raise ValueError("proposal vocabulary size does not match Target")
        artifact = io.artifacts.get(artifact_id)
        if artifact is None:
            artifact = _Artifact(source=source)
            io.artifacts[artifact_id] = artifact
            artifact.operation = asyncio.run_coroutine_threadsafe(
                io._receive(artifact), io.loop
            )
        elif artifact.source != source:
            raise ValueError("artifact identity was reused for a different publication")
        if not artifact.operation.done():
            return {"ready": False}
        artifact.operation.result()
        return {"ready": True}

    def dt_stage(self, artifact_id: str, request_id: str, generation: int) -> None:
        """Bind a completed read to the exact request before Scheduler admission."""
        io = self.dt_io
        artifact = io.artifacts[artifact_id]
        if artifact.operation is None or not artifact.operation.done():
            raise RuntimeError("proposal transfer has not completed")
        artifact.operation.result()
        io.staged[request_id] = (artifact_id, generation)

    def dt_release(self, artifact_id: str, publication_id: str | None = None) -> None:
        """Schedule safe release; source storage requires its completed-read ACK."""
        io = self.dt_io
        artifact = io.artifacts.get(artifact_id)
        if artifact is None:
            return
        # RPCs and Runner hooks share the Worker execution thread. Stop future
        # capture immediately, even if abort cleanup reaches the Runner later.
        for req_id, (source_id, _) in tuple(io.requests.items()):
            if source_id == artifact_id:
                io.requests.pop(req_id)
        if (
            io.capture_proposals
            and artifact.operation is not None
            and publication_id is None
        ):
            # Publication may still be fencing its GPU producer. An abort is
            # not a read ACK, so leave ownership intact until the actual ACK.
            return
        if publication_id is not None:
            artifact.acknowledgement = publication_id
        if artifact.release is not None:
            if not artifact.release.done():
                return
            artifact.release.result()
        for req_id, (staged_id, _) in tuple(io.staged.items()):
            if staged_id == artifact_id:
                io.staged.pop(req_id)
        artifact.release = asyncio.run_coroutine_threadsafe(
            io._release(artifact_id, artifact), io.loop
        )

    def dt_close(self) -> dict[str, Any]:
        """Poll role teardown; unacknowledged publications remain process-owned."""
        io = self.dt_io
        for artifact_id in tuple(io.artifacts):
            self.dt_release(artifact_id)
        if io.artifacts:
            return {"ready": False, "retained_artifacts": len(io.artifacts)}
        if io.close_operation is None:
            io.close_operation = asyncio.run_coroutine_threadsafe(
                io.transport.close(), io.loop
            )
        if not io.close_operation.done():
            return {"ready": False, "retained_artifacts": 0}
        io.close_operation.result()
        io.idle_buffers.clear()
        io.loop.call_soon_threadsafe(io.loop.stop)
        return {"ready": True, "retained_artifacts": 0}

    def dt_status(self) -> dict[str, int]:
        """Report transport-owned artifacts to the role's admission and drain loop."""
        io = self.dt_io
        artifacts = tuple(io.artifacts.values())
        for artifact in artifacts:
            if artifact.release is not None and artifact.release.done():
                artifact.release.result()
        return {
            "retained_artifacts": len(io.artifacts),
            "published_artifacts": sum(
                artifact.buffer is not None and artifact.buffer.publication is not None
                for artifact in artifacts
            ),
            "pending_transfers": sum(
                artifact.source is not None
                and artifact.operation is not None
                and not artifact.operation.done()
                for artifact in artifacts
            ),
        }
