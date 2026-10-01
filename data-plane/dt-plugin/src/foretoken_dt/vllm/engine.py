# SPDX-License-Identifier: Apache-2.0
# SPDX-FileCopyrightText: Copyright contributors to the Foretoken project

"""Client tickets and narrow EngineCore hooks for the external scheduler."""

from contextlib import aclosing
from dataclasses import dataclass
from functools import wraps

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

    async def add_request(self, request_id, prompt, params, *args, **kwargs):
        """Retain the engine-assigned identity until the owning generator closes."""
        collector = await super().add_request(
            request_id, prompt, params, *args, **kwargs
        )
        self._external_request_ids[request_id] = collector.request_id
        return collector

    async def generate(self, prompt, sampling_params, request_id, *args, **kwargs):
        """Attach tickets after native stop handling; preserve native abort cleanup."""
        output_count = 0
        try:
            async with aclosing(
                super().generate(prompt, sampling_params, request_id, *args, **kwargs)
            ) as outputs:
                async for output in outputs:
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
                    yield output
        finally:
            self._external_request_ids.pop(request_id, None)

    async def submit_external_draft_tokens(
        self, ticket: ExternalDraftRequest, token_ids: list[int]
    ) -> bool:
        """Wake EngineCore through its utility queue; return whether admission succeeded."""
        return await self.engine_core.call_utility_async(
            "submit_external_draft_tokens",
            ticket.request_id,
            ticket.generation,
            token_ids,
        )


def install_engine_patches() -> None:
    """Install process-local hooks; ordinary schedulers retain native behavior."""
    if getattr(EngineCore, "_foretoken_external_installed", False):
        return
    original_has_work = EngineCoreProc.has_work

    def submit(self, request_id: str, generation: int, token_ids: list[int]) -> bool:
        """Dispatch ready candidates on the same loop that owns the scheduler."""
        return self.scheduler.submit_external_draft_tokens(
            request_id, generation, token_ids
        )

    @wraps(original_has_work)
    def has_work(self) -> bool:
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

    EngineCore.submit_external_draft_tokens = submit
    EngineCoreProc.has_work = has_work
    EngineCore._foretoken_external_installed = True
