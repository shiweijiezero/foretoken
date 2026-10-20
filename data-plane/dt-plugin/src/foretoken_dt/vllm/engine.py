# SPDX-License-Identifier: Apache-2.0
# SPDX-FileCopyrightText: Copyright contributors to the Foretoken project

"""Client tickets and narrow EngineCore hooks for the external scheduler."""

import asyncio
import time
from collections.abc import Callable
from concurrent.futures import Future
from contextlib import aclosing
from dataclasses import dataclass
from functools import wraps
from queue import SimpleQueue
from typing import Any

from anyio import CancelScope
from vllm.engine.protocol import StreamingInput
from vllm.exceptions import VLLMClientError
from vllm.outputs import STREAM_FINISHED
from vllm.sampling_params import RequestOutputKind
from vllm.utils.async_utils import cancel_task_threadsafe
from vllm.v1.engine import EngineCoreRequestType
from vllm.v1.engine.async_llm import AsyncLLM
from vllm.v1.engine.core import EngineCore, EngineCoreProc

from ..observations import RpcTiming
from .scheduler import DraftScheduler, ExternalScheduler


class DraftAbortedError(RuntimeError):
    """Report native proposal termination so the session owner can distinguish an explicit close."""


@dataclass(frozen=True)
class ExternalDraftRequest:
    """Identify one logical frontier; KV eviction does not consume or replace this ticket."""

    request_id: str
    generation: int


@dataclass
class ExternalDraftSubmission:
    """Identify one candidate chain, its frontier and the session-owned destination when present."""

    ticket: ExternalDraftRequest
    token_ids: list[int]
    work: dict | None = None
    artifact_id: str | None = None
    stage_rpc: RpcTiming | None = None


@dataclass
class _DraftStream:
    """Keep native streaming input and output under one Draft session's abort owner."""

    inputs: asyncio.Queue
    collector: Any = None


@dataclass
class _ReadyRead:
    """Carry completed IO to a Core-only consumer; the session still owns its buffer."""

    apply: Callable[[], dict]
    read: Future
    completion: Future
    ready_at: float


class _Observations:
    """Own one batched native completion reader and its live requests' observation queues."""

    def __init__(self, core):
        """Start the role's sole completion reader; engine shutdown cancels it."""
        self.core = core
        self.queues: dict[str, asyncio.Queue] = {}
        self.reader = asyncio.create_task(self._receive())

    async def _receive(self):
        """Receive existing utility Future notifications independently of request progress."""
        while True:
            batch = await self.core.call_utility_async("dt_next_observations")
            for request_id, generation, observation in batch:
                queue = self.queues.get(request_id)
                if queue is not None:
                    queue.put_nowait((generation, observation))

    async def take(self, request_id, generation, finished):
        """Join the matching output's already-published facts, including stop truncation."""
        queue = self.queues[request_id]
        batches = []
        while True:
            if not queue.empty():
                frontier, observation = queue.get_nowait()
            else:
                item = asyncio.create_task(queue.get())
                try:
                    done, _ = await asyncio.wait(
                        (item, self.reader), return_when=asyncio.FIRST_COMPLETED
                    )
                    if self.reader in done:
                        self.reader.result()
                    frontier, observation = item.result()
                finally:
                    item.cancel()
            batches.extend(observation["physical_batches"])
            if frontier >= generation:
                if not finished and frontier != generation:
                    raise ValueError("native observation passed the confirmed frontier")
                return {**observation, "physical_batches": batches}


class ExternalAsyncLLM(AsyncLLM):
    """Expose DT tickets without changing vLLM's cross-process output schema."""

    def __init__(self, *args, **kwargs) -> None:
        super().__init__(*args, **kwargs)
        self._external_request_ids: dict[str, str] = {}
        self._external_admissions: dict[str, asyncio.Task] = {}
        self._draft_streams: dict[str, _DraftStream] = {}
        self._ready_submissions: list[tuple] = []
        self._submission_flush: asyncio.Task | None = None
        self._observations = _Observations(self.engine_core)

    def shutdown(self, timeout=None):
        """Stop the role-owned observation reader with native engine shutdown."""
        observations = getattr(self, "_observations", None)
        if observations is not None:
            cancel_task_threadsafe(observations.reader)
        super().shutdown(timeout)

    async def worker_rpc(self, method: str, args: tuple):
        """Await a local Worker's native completion notification while other requests execute."""
        return await self.engine_core.call_utility_async("dt_worker_rpc", method, args)

    async def _add_request(self, request, *args, **kwargs):
        """Join each native streaming-input send even when its input task is cancelled."""
        self._observations.queues.setdefault(request.request_id, asyncio.Queue())
        if not request.resumable:
            return await super()._add_request(request, *args, **kwargs)
        previous = self.output_processor.request_states.get(request.request_id)
        if previous is not None:
            if previous.input_chunk_queue is not None:
                raise ValueError("Draft output turn has not completed")
            # Input carries the full confirmed prefix for native KV reconciliation.
            # Rebuild only the output turn state, rather than appending that full
            # prefix and unconfirmed candidates to a growing detokenizer history.
            self.output_processor._finish_request(previous)
        task = asyncio.create_task(super()._add_request(request, *args, **kwargs))
        try:
            return await asyncio.shield(task)
        finally:
            with CancelScope(shield=True):
                await asyncio.shield(task)

    async def propose(self, request_id, prompt, params, priority):
        """Execute one turn of a retained native Draft request; abort closes all turns."""
        stream = self._draft_streams.get(request_id)
        if stream is None:
            inputs = asyncio.Queue()

            async def chunks():
                """Feed native streaming input only after Target confirms the next turn."""
                while True:
                    yield await inputs.get()

            stream = _DraftStream(inputs)
            self._draft_streams[request_id] = stream
            stream.inputs.put_nowait(
                StreamingInput(prompt=prompt, sampling_params=params)
            )
            await self.add_request(request_id, chunks(), params, priority=priority)
        else:
            stream.inputs.put_nowait(
                StreamingInput(prompt=prompt, sampling_params=params)
            )
        tokens = []
        while True:
            # Native input streaming keeps finished=false between turns. The
            # per-turn finish reason is the proposal boundary, not session close.
            output = stream.collector.get_nowait() or await stream.collector.get()
            if output is STREAM_FINISHED:
                raise DraftAbortedError("Draft session closed during proposal")
            result = output.outputs[0]
            if result.finish_reason == "abort":
                raise DraftAbortedError("Draft session aborted during proposal")
            tokens.extend(result.token_ids)
            if result.finish_reason is not None:
                output.outputs[0].token_ids = tokens
                output.dt_observation = await self._observations.take(
                    self._external_request_ids[request_id],
                    len(tokens),
                    True,
                )
                return output

    async def add_request(self, request_id, prompt, params, *args, **kwargs):
        """Own native admission through preprocessing; abort must join before fencing execution."""

        async def admit():
            """Publish the native identity even if the generating caller is cancelled."""
            collector = await super(ExternalAsyncLLM, self).add_request(
                request_id, prompt, params, *args, **kwargs
            )
            self._external_request_ids[request_id] = collector.request_id
            stream = self._draft_streams.get(request_id)
            if stream is not None:
                stream.collector = collector
            return collector

        task = asyncio.create_task(admit())
        self._external_admissions[request_id] = task
        try:
            return await asyncio.shield(task)
        except BaseException:
            # A cancelled generator may still be inside native preprocessing.
            # Join the retained admission before aborting, even without a collector.
            with CancelScope(shield=True):
                await self.abort(request_id)
            raise
        finally:
            self._external_admissions.pop(request_id, None)

    async def generate(self, prompt, sampling_params, request_id, *args, **kwargs):
        """Attach tickets after native stop handling; preserve native abort cleanup."""
        output_count = 0
        previous = {}
        try:
            async with aclosing(
                super().generate(prompt, sampling_params, request_id, *args, **kwargs)
            ) as outputs:
                async for output in outputs:
                    # Native abort can emit a terminal notification with no model
                    # step. It has no measurement to consume and no committed output.
                    if output.outputs[0].finish_reason == "abort":
                        return
                    if sampling_params.output_kind == RequestOutputKind.DELTA:
                        output_count += len(output.outputs[0].token_ids)
                    else:
                        output_count = len(output.outputs[0].token_ids)
                    output.external_draft_request = (
                        None
                        if output.finished
                        else ExternalDraftRequest(
                            self._external_request_ids[request_id], output_count
                        )
                    )
                    observation = await self._observations.take(
                        self._external_request_ids[request_id],
                        output_count,
                        output.finished,
                    )
                    # Timings and counts are cumulative in EngineCore; expose this
                    # output interval, including the final stop-truncated interval.
                    cumulative = {
                        key: observation[key]
                        for key in (
                            "queue_seconds",
                            "scheduled_seconds",
                            "remote_wait_seconds",
                            "scheduled_tokens",
                            "verified_tokens",
                            "accepted_tokens",
                            "steps",
                            "preemptions",
                        )
                    }
                    output.dt_observation = {
                        **observation,
                        **{
                            key: value - previous.get(key, 0)
                            for key, value in cumulative.items()
                        },
                    }
                    previous = cumulative
                    yield output
        finally:
            internal_id = self._external_request_ids.pop(request_id, None)
            self._observations.queues.pop(internal_id, None)
            if internal_id is not None and not self.errored:
                with CancelScope(shield=True):
                    await self.engine_core.call_utility_async(
                        "dt_forget_observation", internal_id
                    )

    async def abort(self, request_id, internal=False) -> None:
        """Confirm native cancellation and GPU completion before releasing session capacity."""
        with CancelScope(shield=True):
            if not internal and (
                admission := self._external_admissions.get(request_id)
            ):
                try:
                    await asyncio.shield(admission)
                except VLLMClientError:
                    # The generating caller still receives this input rejection.
                    # Continue native abort/fencing; cleanup failures must propagate.
                    pass
            stream = self._draft_streams.get(request_id)
            if stream is not None and stream.collector is not None:
                collector = stream.collector
                input_task = collector._input_stream_task
                collector.close()
                if input_task is not None:
                    try:
                        await asyncio.shield(input_task)
                    except asyncio.CancelledError:
                        if not input_task.cancelled():
                            raise
            await super().abort(request_id, internal)
            # Both messages use the same EngineCore socket and ordered input queue.
            # Native abort alone returns after sending, before the scheduler handles it.
            await self.collective_rpc("dt_wait_execution")
            if stream is not None and stream.collector is not None:
                # The input task may have been cancelled before its first chunk
                # reached OutputProcessor, in which case native abort has no
                # request to notify. Always unblock this session's reader.
                stream.collector.put(STREAM_FINISHED)

    async def forget_draft(self, request_id):
        """Close a fenced Draft stream after the service has joined its proposal operation."""
        self._draft_streams.pop(request_id, None)
        internal_id = self._external_request_ids.pop(request_id, None)
        self._observations.queues.pop(internal_id, None)
        if internal_id is not None and not self.errored:
            await self.engine_core.call_utility_async(
                "dt_forget_observation", internal_id
            )

    async def workload(self):
        """Read native ready/waiting queues and KV capacity for online placement."""
        return await self.engine_core.call_utility_async("dt_workload")

    async def request_kv(self, request_id):
        """Query live KV pages for a service-owned engine request; absence means no live KV."""
        internal_id = self._external_request_ids.get(request_id)
        if internal_id is None:
            return None
        return await self.engine_core.call_utility_async("dt_request_kv", internal_id)

    async def export_kv(self, ticket, artifact_id):
        """Snapshot one paused generation through EngineCore's serialized execution loop."""
        return await self.engine_core.call_utility_async(
            "dt_export_kv", ticket.request_id, ticket.generation, artifact_id
        )

    async def export_context(self, ticket, artifact_id, start_token, transport):
        """Snapshot the hidden-state prefix belonging to an outstanding Target ticket."""
        return await self.engine_core.call_utility_async(
            "dt_export_context",
            ticket.request_id,
            ticket.generation,
            artifact_id,
            start_token,
            transport,
        )

    async def receive_external_draft(
        self, entry: ExternalDraftSubmission, payload: dict, read_rpc: RpcTiming
    ) -> dict:
        """Read and admit once; the caller retains storage through any uncertain utility result."""
        return await self._receive_and_stage(
            "dt_receive_external_draft",
            (
                entry.ticket.request_id,
                entry.ticket.generation,
                entry.token_ids,
                entry.work,
                entry.artifact_id,
                payload,
            ),
            read_rpc,
            entry.stage_rpc,
        )

    async def receive_draft_context(
        self,
        session_id: str,
        version: int,
        start_token: int,
        end_token: int,
        artifact_id: str,
        payload: dict,
        read_rpc: RpcTiming,
        stage_rpc: RpcTiming,
    ) -> dict:
        """Read and append a retained neural Draft's delta; its session joins ACK and cleanup.

        First turns use the ordinary Worker path until native admission exists.
        A completed read returns its proof even when append is rejected or fails;
        append_error requires abort/fencing before destination reclamation.
        """
        return await self._receive_and_stage(
            "dt_receive_draft_context",
            (
                self._external_request_ids[session_id],
                session_id,
                version,
                start_token,
                end_token,
                artifact_id,
                payload,
            ),
            read_rpc,
            stage_rpc,
        )

    async def _receive_and_stage(self, method, args, read_rpc, stage_rpc):
        """Measure one fused utility's read and subsequent Core consumption as nested phases."""
        sent = time.monotonic()
        result = await self.engine_core.call_utility_async(method, *args)
        received = time.monotonic()
        read = result.pop("read_rpc")
        stage = result.pop("stage")
        # This is one utility: read return includes the Core consumption phase.
        # These nested timings must not be added together.
        read_rpc.record(
            sent,
            received,
            read["started"],
            read["finished"],
        )
        if stage_rpc is not None and stage is not None:
            stage_rpc.record(
                read["ready"], received, stage["started"], stage["finished"]
            )
        return result

    async def submit_external_draft_batch(
        self, entries: list[ExternalDraftSubmission]
    ) -> list[bool]:
        """Coalesce ready token-only chains; native scheduling owns the GPU batch."""
        future = asyncio.get_running_loop().create_future()
        self._ready_submissions.append((entries, future))
        if self._submission_flush is None:
            self._submission_flush = asyncio.create_task(self._flush_submissions())
        return await asyncio.shield(future)

    async def _flush_submissions(self) -> None:
        """Submit current readiness without a batching timer or a wait for unready requests."""
        await asyncio.sleep(0)
        pending, self._ready_submissions = self._ready_submissions, []
        self._submission_flush = None
        entries = [entry for group, _ in pending for entry in group]
        try:
            results = await self.engine_core.call_utility_async(
                "submit_external_draft_batch",
                [
                    (
                        entry.ticket.request_id,
                        entry.ticket.generation,
                        entry.token_ids,
                        entry.work,
                        None,
                    )
                    for entry in entries
                ],
            )
            offset = 0
            for group, future in pending:
                accepted = [
                    result["accepted"]
                    for result in results[offset : offset + len(group)]
                ]
                offset += len(group)
                future.set_result(accepted)
        except Exception as error:  # noqa: BLE001 - each waiting caller must receive the batch failure.
            # Every caller retains its submitted ticket and buffer ownership.
            # An uncertain utility result is an error, never a reason to replay.
            for _, future in pending:
                if not future.done():
                    future.set_exception(error)


def install_engine_patches() -> None:
    """Install process-local hooks; ordinary schedulers retain native behavior."""
    if getattr(EngineCore, "_foretoken_external_installed", False):
        return
    original_has_work = EngineCoreProc.has_work
    original_process_input_queue = EngineCoreProc._process_input_queue

    def worker_rpc(self, method, args):
        """Unwrap the one local Worker so native utility Futures can notify IO completion."""
        (result,) = self.model_executor.collective_rpc(method, args=args)
        return result

    def submit_batch(self, entries) -> list[dict]:
        """Stage all ready members and admit their tickets before native scheduling resumes."""
        results = []
        for request_id, generation, tokens, work, artifact_id in entries:
            if not self.scheduler.can_admit_external_draft(request_id, generation):
                results.append({"accepted": False, "stage": None})
                continue
            stage = None
            if artifact_id is not None:
                (stage,) = self.model_executor.collective_rpc(
                    "dt_timed_rpc",
                    args=("dt_stage", (artifact_id, request_id, generation)),
                )
            accepted = self.scheduler.submit_external_draft_tokens(
                request_id, generation, tokens, work
            )
            results.append({"accepted": accepted, "stage": stage})
        return results

    def receive_external_draft(
        self, request_id, generation, tokens, work, artifact_id, payload
    ) -> Future:
        """Start one read; IO completion wakes the native input boundary for ticket admission."""

        def admit():
            """Consume the Target ticket on Core after queued native aborts have run."""
            (result,) = submit_batch(
                self, [(request_id, generation, tokens, work, artifact_id)]
            )
            return result

        return start_read(self, artifact_id, payload, admit)

    def receive_draft_context(
        self,
        request_id,
        session_id,
        version,
        start_token,
        end_token,
        artifact_id,
        payload,
    ) -> Future:
        """Append a completed feature read only while its retained Draft still awaits this turn."""

        def append():
            """Validate the retained turn and keep read proof through any append failure."""
            result = {"appended": False, "append_error": None, "stage": None}
            request = self.scheduler.requests.get(request_id)
            if (
                not isinstance(self.scheduler, DraftScheduler)
                or request is None
                or request.is_finished()
                or not request.dt_draft_waiting
            ):
                return result
            extra = request.sampling_params.extra_args
            if (
                extra.get("dt_context_session") != session_id
                or version <= extra["dt_version"]
                or start_token != len(request.prompt_token_ids)
                or end_token - start_token != version - extra["dt_version"]
            ):
                return result
            try:
                (stage,) = self.model_executor.collective_rpc(
                    "dt_timed_rpc",
                    args=(
                        "dt_append_draft_context",
                        (session_id, artifact_id, start_token, end_token),
                    ),
                )
            except Exception as error:  # noqa: BLE001 - preserve the completed read for source ACK.
                result["append_error"] = f"{type(error).__name__}: {error}"
            else:
                result.update(appended=True, stage=stage)
            return result

        return start_read(self, artifact_id, payload, append)

    def start_read(self, artifact_id, payload, apply) -> Future:
        """Queue one completed read's Core consumer; IO threads never mutate inference state."""
        ready = getattr(self, "_dt_ready_reads", None)
        if ready is None:
            ready = self._dt_ready_reads = SimpleQueue()
        completion = Future()
        (read,) = self.model_executor.collective_rpc(
            "dt_timed_rpc", args=("dt_receive", (artifact_id, payload))
        )

        def read_completed(read):
            # The transport thread only publishes readiness. Scheduler and
            # Worker bindings remain serialized with native abort/preemption.
            ready.put(
                _ReadyRead(
                    apply,
                    read,
                    completion,
                    time.monotonic(),
                )
            )
            self.input_queue.put_nowait((EngineCoreRequestType.WAKEUP, None))

        read.add_done_callback(read_completed)
        return completion

    @wraps(original_process_input_queue)
    def process_input_queue(self):
        """Drain native input and aborts before consuming individually completed reads."""
        original_process_input_queue(self)
        ready = getattr(self, "_dt_ready_reads", None)
        while ready is not None and not ready.empty():
            pending = ready.get_nowait()
            try:
                read = pending.read.result()
                result = pending.apply()
                pending.completion.set_result(
                    {
                        **read["value"],
                        **result,
                        "read_rpc": {
                            "started": read["started"],
                            "finished": read["finished"],
                            "ready": pending.ready_at,
                        },
                    }
                )
            except Exception as error:  # noqa: BLE001 - complete each native utility independently.
                pending.completion.set_exception(error)

    @wraps(original_has_work)
    def has_work(self) -> bool:
        if (
            self.vllm_config.additional_config.get("foretoken_dt_context_role")
            == "draft"
        ):
            self.check_for_draft_tokens = False
        if isinstance(self.scheduler, (DraftScheduler, ExternalScheduler)):
            # Plugins can load during EngineCore construction. Set this before
            # the busy loop steps rather than wrapping an already-entered init.
            self.check_for_draft_tokens = False
            return bool(
                self.engines_running
                or self.scheduler.has_schedulable_requests()
                or self.batch_queue
                or (
                    (ready := getattr(self, "_dt_ready_reads", None)) is not None
                    and not ready.empty()
                )
            )
        return original_has_work(self)

    def workload(self):
        return self.scheduler.dt_workload()

    def request_kv(self, request_id):
        return self.scheduler.dt_request_kv(request_id)

    def next_observations(self):
        return self.scheduler.dt_next_observations()

    def forget_observation(self, request_id):
        self.scheduler.dt_observations.pop(request_id, None)
        if (
            self.vllm_config.additional_config.get("foretoken_dt_context_role")
            == "target"
        ):
            self.model_executor.collective_rpc("dt_forget_context", args=(request_id,))

    def export_kv(self, request_id, generation, artifact_id):
        """Copy resident KV atomically with respect to native scheduling and preemption."""
        from math import ceil

        request = self.scheduler.requests.get(request_id)
        if (
            request is None
            or request.is_finished()
            or not request.waiting_for_external_draft
            or request.num_output_tokens != generation
            or request.num_computed_tokens == 0
        ):
            return None
        manager = self.scheduler.kv_cache_manager
        computed = min(request.num_computed_tokens, request.num_tokens - 1)
        block_size = manager.kv_cache_config.kv_cache_groups[0].kv_cache_spec.block_size
        blocks = manager.get_blocks(request_id).get_block_ids()[0][
            : ceil(computed / block_size)
        ]
        layout = self.model_executor.collective_rpc(
            "dt_snapshot_kv", args=(artifact_id, blocks)
        )[0]
        return {
            "artifact_id": artifact_id,
            "computed_tokens": computed,
            "token_ids": list(request.all_token_ids),
            "layout": layout,
        }

    def export_context(
        self, request_id, generation, artifact_id, start_token, transport
    ):
        """Validate the frontier and initiate feature publication in one native utility turn."""
        request = self.scheduler.requests.get(request_id)
        if (
            request is None
            or request.is_finished()
            or not request.waiting_for_external_draft
            or request.num_output_tokens != generation
        ):
            return None
        computed = request.num_tokens - 1
        if not 0 <= start_token < computed:
            raise ValueError("context delta must extend a confirmed feature prefix")
        metadata = {
            "artifact_id": artifact_id,
            "token_ids": list(request.all_token_ids),
            "start_token": start_token,
            "end_token": computed,
        }
        return self.model_executor.collective_rpc(
            "dt_snapshot_context",
            args=(artifact_id, request_id, start_token, computed, transport, metadata),
        )[0]

    EngineCore.dt_workload = workload
    EngineCore.dt_worker_rpc = worker_rpc
    EngineCore.dt_request_kv = request_kv
    EngineCore.dt_next_observations = next_observations
    EngineCore.dt_forget_observation = forget_observation
    EngineCore.dt_export_kv = export_kv
    EngineCore.dt_export_context = export_context
    EngineCore.dt_receive_external_draft = receive_external_draft
    EngineCore.dt_receive_draft_context = receive_draft_context
    EngineCore.submit_external_draft_batch = submit_batch
    EngineCoreProc.has_work = has_work
    EngineCoreProc._process_input_queue = process_input_queue
    EngineCore._foretoken_external_installed = True
