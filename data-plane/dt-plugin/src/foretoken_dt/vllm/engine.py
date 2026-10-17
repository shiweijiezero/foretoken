# SPDX-License-Identifier: Apache-2.0
# SPDX-FileCopyrightText: Copyright contributors to the Foretoken project

"""Client tickets and narrow EngineCore hooks for the external scheduler."""

import asyncio
from contextlib import aclosing
from dataclasses import dataclass
from functools import wraps

from anyio import CancelScope
from vllm.sampling_params import RequestOutputKind
from vllm.v1.engine.async_llm import AsyncLLM
from vllm.v1.engine.core import EngineCore, EngineCoreProc

from .scheduler import ExternalScheduler


@dataclass(frozen=True)
class ExternalDraftRequest:
    """Identify a unique engine request and its confirmed output-token frontier."""

    request_id: str
    generation: int


class ExternalAsyncLLM(AsyncLLM):
    """Expose DT tickets without changing vLLM's cross-process output schema."""

    def __init__(self, *args, **kwargs) -> None:
        super().__init__(*args, **kwargs)
        self._external_request_ids: dict[str, str] = {}
        self._external_admissions: dict[str, asyncio.Task] = {}

    async def add_request(self, request_id, prompt, params, *args, **kwargs):
        """Own native admission through preprocessing; abort must join before fencing execution."""

        async def admit():
            """Publish the native identity even if the generating caller is cancelled."""
            collector = await super(ExternalAsyncLLM, self).add_request(
                request_id, prompt, params, *args, **kwargs
            )
            self._external_request_ids[request_id] = collector.request_id
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
                    observation = await self.engine_core.call_utility_async(
                        "dt_take_observation",
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
                await asyncio.shield(admission)
            await super().abort(request_id, internal)
            # Both messages use the same EngineCore socket and ordered input queue.
            # Native abort alone returns after sending, before the scheduler handles it.
            await self.collective_rpc("dt_wait_execution")

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

    async def export_context(self, ticket, artifact_id):
        """Snapshot the hidden-state prefix belonging to an outstanding Target ticket."""
        return await self.engine_core.call_utility_async(
            "dt_export_context", ticket.request_id, ticket.generation, artifact_id
        )

    async def submit_external_draft_batch(
        self, entries: list[tuple[ExternalDraftRequest, list[int], dict]]
    ) -> list[bool]:
        """Admit a ready batch in one EngineCore turn, before its scheduler runs."""
        return await self.engine_core.call_utility_async(
            "submit_external_draft_batch",
            [
                (ticket.request_id, ticket.generation, tokens, work)
                for ticket, tokens, work in entries
            ],
        )


def install_engine_patches() -> None:
    """Install process-local hooks; ordinary schedulers retain native behavior."""
    if getattr(EngineCore, "_foretoken_external_installed", False):
        return
    original_has_work = EngineCoreProc.has_work

    def submit_batch(
        self, entries: list[tuple[str, int, list[int], dict]]
    ) -> list[bool]:
        """Release all valid members together; native scheduling still owns GPU capacity."""
        return [
            self.scheduler.submit_external_draft_tokens(
                request_id, generation, tokens, work
            )
            for request_id, generation, tokens, work in entries
        ]

    @wraps(original_has_work)
    def has_work(self) -> bool:
        if (
            self.vllm_config.additional_config.get("foretoken_dt_context_role")
            == "draft"
        ):
            self.check_for_draft_tokens = False
        if isinstance(self.scheduler, ExternalScheduler):
            # Plugins can load during EngineCore construction. Set this before
            # the busy loop steps rather than wrapping an already-entered init.
            self.check_for_draft_tokens = False
            return bool(
                self.engines_running
                or self.scheduler.has_schedulable_requests()
                or self.batch_queue
            )
        return original_has_work(self)

    def workload(self):
        return self.scheduler.dt_workload()

    def request_kv(self, request_id):
        return self.scheduler.dt_request_kv(request_id)

    def take_observation(self, request_id, generation, finished):
        return self.scheduler.dt_take_observation(request_id, generation, finished)

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

    def export_context(self, request_id, generation, artifact_id):
        """Resolve confirmed positions on the scheduler thread before copying features."""
        request = self.scheduler.requests.get(request_id)
        if (
            request is None
            or request.is_finished()
            or not request.waiting_for_external_draft
            or request.num_output_tokens != generation
        ):
            return None
        computed = request.num_tokens - 1
        self.model_executor.collective_rpc(
            "dt_snapshot_context", args=(artifact_id, request_id, computed)
        )
        return {"artifact_id": artifact_id, "token_ids": list(request.all_token_ids)}

    EngineCore.dt_workload = workload
    EngineCore.dt_request_kv = request_kv
    EngineCore.dt_take_observation = take_observation
    EngineCore.dt_forget_observation = forget_observation
    EngineCore.dt_export_kv = export_kv
    EngineCore.dt_export_context = export_context
    EngineCore.submit_external_draft_batch = submit_batch
    EngineCoreProc.has_work = has_work
    EngineCore._foretoken_external_installed = True
