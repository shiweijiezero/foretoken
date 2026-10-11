# SPDX-License-Identifier: Apache-2.0
# SPDX-FileCopyrightText: Copyright contributors to the Foretoken project

"""Worker-owned proposal distributions and asynchronous Mooncake transfers."""

from __future__ import annotations

import asyncio
import time
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
    tensor: torch.Tensor | None = None
    event: torch.cuda.Event | None = None
    buffer: RegisteredTensor | None = None
    operation: Future[Any] | None = None
    release: Future[Any] | None = None
    source: PayloadRef | None = None
    acknowledgement: str | None = None
    claimed: bool = False
    read_seconds: float | None = None


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
        self.idle_buffers: dict[tuple, list[RegisteredTensor]] = {}
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

    def on_neural_proposal(self, batch, tokens, logits, temperatures, limits):
        """Publish the native speculator's actual q for completed Draft prefills."""
        for index, req_id in enumerate(batch.req_ids):
            if (
                batch.num_computed_tokens_np[index] + batch.num_scheduled_tokens[index]
                < batch.prefill_len_np[index]
            ):
                continue
            binding = self.requests.get(req_id)
            if binding is None:
                continue
            artifact_id, greedy = binding
            slot = int(batch.idx_mapping_np[index])
            count = limits[index]
            if greedy:
                rows = torch.full(
                    (count, self.vocab_size),
                    -torch.inf,
                    dtype=torch.float32,
                    device=self.device,
                )
                rows.scatter_(1, tokens[index, :count, None].long(), 0.0)
            else:
                rows = torch.log_softmax(
                    logits[slot, :count].float() / temperatures[slot], dim=-1
                )
            artifact = self.artifacts[artifact_id]
            artifact.tensor = rows
            artifact.event = torch.cuda.Event()
            artifact.event.record(torch.cuda.current_stream(self.device))

    def _acquire_buffer(
        self, shape: tuple[int, ...], dtype=torch.float32
    ) -> RegisteredTensor:
        """Borrow registered tensor capacity on the transport loop, retaining its RDMA key."""
        if len(shape) == 3:
            # Feature prefixes grow every round. Geometric row capacity bounds
            # retained registrations by context size rather than its square.
            shape = (1 << (shape[0] - 1).bit_length(), *shape[1:])
        idle = self.idle_buffers.setdefault((shape, dtype), [])
        if idle:
            return idle.pop()
        tensor = torch.empty(shape, dtype=dtype, device=self.device)
        return self.transport.register(tensor)

    def publish_tensor(self, artifact_id: str, tensor: torch.Tensor) -> None:
        """Retain a device snapshot and its compute fence until publication or cancellation."""
        if artifact_id in self.artifacts:
            raise ValueError("artifact identity is already in use")
        event = torch.cuda.Event()
        event.record(torch.cuda.current_stream(self.device))
        self.artifacts[artifact_id] = _Artifact(tensor=tensor, event=event)

    async def _publish(self, artifact: _Artifact) -> dict[str, Any]:
        """Fence captured rows and fill a reusable publication buffer for Target."""
        with torch.cuda.device(self.device):
            if artifact.event is not None:
                await asyncio.to_thread(artifact.event.synchronize)
            if artifact.tensor is None:
                artifact.buffer = self._acquire_buffer(
                    (len(artifact.rows), self.vocab_size)
                )
                torch.stack(artifact.rows, out=artifact.buffer.tensor)
            else:
                artifact.buffer = self._acquire_buffer(
                    tuple(artifact.tensor.shape), artifact.tensor.dtype
                )
                artifact.buffer.tensor[: artifact.tensor.shape[0]].copy_(
                    artifact.tensor
                )
            event = torch.cuda.Event()
            event.record(torch.cuda.current_stream(self.device))
            payload = await artifact.buffer.publish(
                ready_event=event,
                rows=None if artifact.tensor is None else artifact.tensor.shape[0],
            )
            artifact.rows.clear()
            artifact.tensor = None
            return payload.to_wire()

    async def _receive(self, artifact: _Artifact) -> None:
        """Borrow local registered storage and await RDMA outside execution."""
        source = artifact.source
        with torch.cuda.device(self.device):
            dtype = getattr(torch, source.dtype)
            artifact.buffer = self._acquire_buffer(source.shape, dtype)
            event = torch.cuda.Event()
            event.record(torch.cuda.current_stream(self.device))
            started = time.monotonic()
            await artifact.buffer.read(source, ready_event=event)
            artifact.read_seconds = time.monotonic() - started

    async def _release(self, artifact_id: str, artifact: _Artifact) -> None:
        """Fence local use and recycle revoked or acknowledged source storage."""
        if artifact.operation is not None:
            await asyncio.wrap_future(artifact.operation)
        buffer = artifact.buffer
        if buffer is not None:
            if buffer.publication is not None:
                # An unclaimed descriptor never left the source Worker. Its
                # release RPC already prevents any later claim from succeeding.
                buffer.release_source(
                    buffer.publication.publication_id
                    if artifact.acknowledgement is None
                    else artifact.acknowledgement
                )
            if artifact.event is not None:
                await asyncio.to_thread(artifact.event.synchronize)
            # The operation future proves DMA completion; this event fences the
            # last local GPU consumer. Failed reads never reach the idle pool.
            artifact.buffer = None
            self.idle_buffers[(tuple(buffer.tensor.shape), buffer.tensor.dtype)].append(
                buffer
            )
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
        if self.vllm_config.kv_transfer_config is not None:
            from vllm.distributed.kv_transfer import get_kv_transfer_group

            get_kv_transfer_group().io = self.dt_io

    def dt_snapshot_kv(self, artifact_id: str, block_ids: list[int]) -> list:
        """Copy a scheduler-selected checkpoint before native blocks can be reused."""
        from vllm.distributed.kv_transfer import get_kv_transfer_group

        return get_kv_transfer_group().snapshot(artifact_id, block_ids)

    def dt_snapshot_context(
        self, artifact_id: str, request_id: str, computed_tokens: int
    ) -> None:
        """Snapshot features selected by the paused Target's scheduler frontier."""
        tensor = self.model_runner.speculator.snapshot(request_id, computed_tokens)
        self.dt_io.publish_tensor(artifact_id, tensor)

    def dt_forget_context(self, request_id: str) -> None:
        """Release retained features on client completion, including an idle last request."""
        self.model_runner.speculator.requests.pop(request_id, None)

    def dt_validate_kv(self, layout: list, nbytes: int, computed_tokens: int) -> None:
        """Reject foreign layout or size before allocating a receiver buffer."""
        from math import ceil

        from vllm.distributed.kv_transfer import get_kv_transfer_group

        connector = get_kv_transfer_group()
        count = ceil(computed_tokens / connector.block_size)
        expected = sum(
            count * cache[0].numel() * cache.element_size()
            for cache in connector.caches.values()
        )
        if layout != connector.layout() or nbytes != expected:
            raise ValueError("KV checkpoint layout differs from destination")

    def dt_publish(self, artifact_id: str) -> dict[str, Any]:
        """Prepare a publication; only dt_claim may expose its descriptor."""
        io = self.dt_io
        artifact = io.artifacts[artifact_id]
        if artifact.operation is None:
            if not artifact.rows and artifact.tensor is None:
                raise ValueError("Draft artifact has no sampled distributions")
            artifact.operation = asyncio.run_coroutine_threadsafe(
                io._publish(artifact), io.loop
            )
        if not artifact.operation.done():
            return {"ready": False}
        artifact.operation.result()
        return {"ready": True}

    def dt_claim(self, artifact_id: str) -> dict[str, Any] | None:
        """Give one Target read ownership, serialized with cancellation RPCs."""
        artifact = self.dt_io.artifacts.get(artifact_id)
        if artifact is None or artifact.release is not None or artifact.claimed:
            return None
        if artifact.operation is None or not artifact.operation.done():
            return None
        payload = artifact.operation.result()
        artifact.claimed = True
        return payload

    def dt_receive(self, artifact_id: str, payload: dict[str, Any]) -> dict[str, Any]:
        """Start or poll one immutable publication's RDMA read on this Worker."""
        io = self.dt_io
        source = PayloadRef.from_wire(payload)
        if source.dtype == "uint8" and len(source.shape) == 1:
            if self.vllm_config.kv_transfer_config is None:
                raise ValueError("KV migration is not enabled")
        elif self.model_runner.context_role == "draft" and len(source.shape) == 3:
            from vllm.v1.worker.gpu.spec_decode.eagle.eagle3_utils import (
                get_eagle3_aux_layers_from_config,
            )

            model = self.model_runner.model
            layers = get_eagle3_aux_layers_from_config(
                self.vllm_config.speculative_config
            )
            if not self.model_runner.use_aux_hidden_state_outputs:
                layers = ()
            elif not layers:
                layers = model.get_eagle3_default_aux_hidden_state_layers()
            expected = (
                1 + len(layers),
                self.vllm_config.model_config.get_hidden_size(),
            )
            if (
                source.shape[1:] != expected
                or source.shape[0] > self.model_runner.max_model_len
            ):
                raise ValueError(
                    "Target feature dimensions do not match the native speculator"
                )
            if source.dtype != str(self.vllm_config.model_config.dtype).removeprefix(
                "torch."
            ):
                raise ValueError("Target features have a different dtype")
        elif source.dtype != "float32" or len(source.shape) != 2:
            raise ValueError(
                "proposal distribution requires float32 [tokens, vocabulary]"
            )
        elif source.shape[1] != io.vocab_size:
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
        return {
            "ready": True,
            "read_seconds": artifact.read_seconds,
            "bytes": source.nbytes,
        }

    def dt_stage(self, artifact_id: str, request_id: str, generation: int) -> None:
        """Bind a completed read to the exact request before Scheduler admission."""
        io = self.dt_io
        artifact = io.artifacts[artifact_id]
        if artifact.operation is None or not artifact.operation.done():
            raise RuntimeError("proposal transfer has not completed")
        artifact.operation.result()
        io.staged[request_id] = (artifact_id, generation)

    def dt_release(self, artifact_id: str, publication_id: str | None = None) -> None:
        """Revoke unclaimed storage; claimed sources wait for their reader ACK."""
        io = self.dt_io
        artifact = io.artifacts.get(artifact_id)
        if artifact is None:
            return
        # RPCs and Runner hooks share the Worker execution thread. Stop future
        # capture immediately, even if abort cleanup reaches the Runner later.
        for req_id, (source_id, _) in tuple(io.requests.items()):
            if source_id == artifact_id:
                io.requests.pop(req_id)
        if artifact.claimed and publication_id is None:
            # Target owns a descriptor and may still start or finish its read.
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
            "unacknowledged_publications": sum(
                artifact.claimed and artifact.acknowledgement is None
                for artifact in artifacts
            ),
            "pending_transfers": sum(
                artifact.source is not None
                and artifact.operation is not None
                and not artifact.operation.done()
                for artifact in artifacts
            ),
        }
