# SPDX-License-Identifier: Apache-2.0
# SPDX-FileCopyrightText: Copyright contributors to the Foretoken project

"""Greedy role execution; placement and round orchestration belong to the frontend."""

from __future__ import annotations

import asyncio
import json
import time
from contextlib import aclosing, asynccontextmanager
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Literal
from uuid import uuid4

from anyio import CancelScope
from fastapi import FastAPI, HTTPException
from fastapi.responses import StreamingResponse
from pydantic import BaseModel, ConfigDict, Field, StrictInt

if TYPE_CHECKING:
    from vllm import AsyncEngineArgs
    from vllm.outputs import ExternalDraftRequest
    from vllm.v1.engine.async_llm import AsyncLLM


class Message(BaseModel):
    """Explicit internal HTTP request schema; unknown fields are rejected."""

    model_config = ConfigDict(extra="forbid")


class OpenDraft(Message):
    """Confirmed token context supplied by the frontend when binding a Draft."""

    token_ids: list[StrictInt] = Field(min_length=1)
    version: StrictInt = Field(ge=0)


class Propose(Message):
    """One greedy candidate chain from an already confirmed Draft context."""

    version: StrictInt = Field(ge=0)
    max_tokens: StrictInt = Field(gt=0)


class Commit(Message):
    """Target-confirmed delta, never a Draft's guess at the accepted prefix."""

    base_version: StrictInt = Field(ge=0)
    version: StrictInt = Field(ge=0)
    token_ids: list[StrictInt] = Field(min_length=1)


class Generate(Message):
    """Token-input Target generation; final stopping remains owned by vLLM."""

    token_ids: list[StrictInt] = Field(min_length=1)
    max_tokens: StrictInt = Field(gt=0)
    stop: list[str] = Field(default_factory=list)
    stop_token_ids: list[StrictInt] = Field(default_factory=list)
    min_tokens: StrictInt = Field(default=0, ge=0)
    ignore_eos: bool = False


class Verify(Message):
    """Ready candidates for the Target's currently published round ticket."""

    version: StrictInt = Field(ge=0)
    token_ids: list[StrictInt]


@dataclass
class DraftSession:
    """Connection-owned confirmed prefix; vLLM owns all cached model state."""

    token_ids: list[int]
    version: int
    closed: asyncio.Event = field(default_factory=asyncio.Event)
    active_request: str | None = None


def _event(value: dict) -> bytes:
    """Encode an explicit control event; no model tensors enter this protocol."""
    return (json.dumps(value) + "\n").encode()


class RoleService:
    """Own one engine and its connection-bound sessions until role shutdown."""

    def __init__(self, engine: AsyncLLM, role: Literal["draft", "target"], budget: int):
        self.engine = engine
        self.role = role
        self.budget = budget
        self.accepting = True
        self.drafts: dict[str, DraftSession] = {}
        self.targets: dict[str, ExternalDraftRequest | None] = {}

    def require_role(self, role: str) -> None:
        """Reject operations sent to the wrong bound role."""
        if self.role != role:
            raise HTTPException(409, f"operation requires a {role} role")

    def require_accepting(self) -> None:
        """Gate new sessions only; existing rounds remain executable during drain."""
        if self.engine.errored:
            raise HTTPException(503, "role engine is unavailable")
        if not self.accepting:
            raise HTTPException(503, "role is draining")

    def draft_session(self, session_id: str) -> DraftSession:
        """Resolve a live local session; closed sessions cannot be resurrected."""
        self.require_role("draft")
        session = self.drafts.get(session_id)
        if session is None:
            raise HTTPException(404, "draft session is closed or unknown")
        return session

    def validate_tokens(self, tokens: list[int]) -> None:
        """Reject out-of-vocabulary control input before mutating session state."""
        vocab_size = self.engine.model_config.get_vocab_size()
        if any(token < 0 or token >= vocab_size for token in tokens):
            raise HTTPException(422, "token IDs are outside this model's vocabulary")

    async def close_session(self, session_id: str) -> None:
        """Remove one session and abort its model work, including during drain."""
        # StreamingResponse cancels an AnyIO scope on disconnect. Cleanup must
        # reach EngineCore even inside that cancelled scope.
        with CancelScope(shield=True):
            session = self.drafts.pop(session_id, None)
            if session is not None:
                session.closed.set()
                if session.active_request is not None:
                    await self.engine.abort(session.active_request)
            if session_id in self.targets:
                del self.targets[session_id]
                await self.engine.abort(session_id)

    async def open_draft(self, request: OpenDraft):
        """Keep the owning control stream open; disconnect closes the Draft session."""
        self.require_accepting()
        session_id = uuid4().hex
        session = DraftSession(list(request.token_ids), request.version)
        self.drafts[session_id] = session
        try:
            yield _event({"event": "opened", "session_id": session_id})
            await session.closed.wait()
        finally:
            await self.close_session(session_id)

    async def propose(self, session_id: str, request: Propose) -> dict:
        """Run one locally batched Draft request against its confirmed token prefix.

        Each round is a normal vLLM request. Prefix caching reuses immutable full
        blocks; rejected suffixes are never committed into the next round's prompt.
        Closing the owning session aborts in-flight generation.
        """
        from vllm import SamplingParams
        from vllm.inputs import TokensPrompt
        from vllm.sampling_params import RequestOutputKind

        session = self.draft_session(session_id)
        if request.version != session.version or session.active_request is not None:
            raise HTTPException(409, "context version changed or proposal is in flight")
        if request.max_tokens > self.budget:
            raise HTTPException(422, "proposal exceeds the role's token budget")
        request_id = uuid4().hex
        session.active_request = request_id
        try:
            params = SamplingParams(
                temperature=0,
                max_tokens=request.max_tokens,
                ignore_eos=True,
                output_kind=RequestOutputKind.FINAL_ONLY,
            )
            tokens = None
            async with aclosing(
                self.engine.generate(
                    TokensPrompt(prompt_token_ids=list(session.token_ids)),
                    params,
                    request_id,
                )
            ) as outputs:
                async for output in outputs:
                    tokens = list(output.outputs[0].token_ids)
            if session.closed.is_set():
                raise HTTPException(409, "session closed during proposal")
            if tokens is None:
                raise RuntimeError("Draft engine completed without a candidate")
            return {"version": session.version, "token_ids": tokens}
        finally:
            with CancelScope(shield=True):
                await self.engine.abort(request_id)
                session.active_request = None

    async def generate(self, request: Generate):
        """Stream Target-confirmed deltas; disconnect aborts the original request."""
        from vllm import SamplingParams
        from vllm.inputs import TokensPrompt
        from vllm.sampling_params import RequestOutputKind

        self.require_accepting()
        session_id = uuid4().hex
        self.targets[session_id] = None
        try:
            yield _event({"event": "opened", "session_id": session_id})
            params = SamplingParams(
                temperature=0,
                max_tokens=request.max_tokens,
                stop=request.stop,
                stop_token_ids=request.stop_token_ids,
                min_tokens=request.min_tokens,
                ignore_eos=request.ignore_eos,
                output_kind=RequestOutputKind.DELTA,
            )
            async with aclosing(
                self.engine.generate(
                    TokensPrompt(prompt_token_ids=request.token_ids), params, session_id
                )
            ) as outputs:
                async for output in outputs:
                    if session_id not in self.targets:
                        return
                    ticket = output.external_draft_request
                    self.targets[session_id] = ticket
                    completion = output.outputs[0]
                    yield _event(
                        {
                            "event": "committed",
                            "token_ids": list(completion.token_ids),
                            "text": completion.text,
                            "version": ticket.generation
                            if ticket is not None
                            else None,
                            "finished": output.finished,
                            "finish_reason": completion.finish_reason,
                            "stop_reason": completion.stop_reason,
                            "cached_token_count": output.num_cached_tokens or 0,
                        }
                    )
        finally:
            await self.close_session(session_id)

    async def verify(self, session_id: str, request: Verify) -> dict:
        """Forward ready candidates; vLLM is the authority for ticket consumption."""
        self.require_role("target")
        if session_id not in self.targets:
            raise HTTPException(404, "target session is closed or unknown")
        ticket = self.targets[session_id]
        if ticket is None or ticket.generation != request.version:
            return {"accepted": False}
        if len(request.token_ids) > self.budget:
            raise HTTPException(422, "candidate exceeds the role's token budget")
        self.validate_tokens(request.token_ids)
        accepted = await self.engine.submit_external_draft_tokens(
            ticket, request.token_ids
        )
        return {"accepted": accepted}


def create_app(
    engine_args: AsyncEngineArgs, role: Literal["draft", "target"], budget: int
) -> FastAPI:
    """Build an internal role API; its lifespan exclusively owns the vLLM engine."""
    from vllm.v1.engine.async_llm import AsyncLLM

    @asynccontextmanager
    async def lifespan(app):
        engine = AsyncLLM.from_engine_args(engine_args)
        service = RoleService(engine, role, budget)
        app.state.service = service
        try:
            yield
        finally:
            service.accepting = False
            try:
                for session_id in list(service.drafts) + list(service.targets):
                    await service.close_session(session_id)
            finally:
                engine.shutdown()

    app = FastAPI(lifespan=lifespan)

    @app.get("/status")
    async def status():
        """Expose readiness, model identity and session count for role placement."""
        from vllm.v1.engine.exceptions import EngineDeadError

        service = app.state.service
        try:
            await service.engine.check_health()
        except EngineDeadError as error:
            raise HTTPException(503, "role engine is unavailable") from error
        return {
            "role": role,
            "model": service.engine.model_config.model,
            "revision": service.engine.model_config.revision,
            "tokenizer": service.engine.model_config.tokenizer,
            "tokenizer_revision": service.engine.model_config.tokenizer_revision,
            "max_model_len": service.engine.model_config.max_model_len,
            "accepting": service.accepting,
            "active_sessions": len(service.drafts) + len(service.targets),
            "candidate_format": "greedy_token_ids",
            "token_budget": budget,
        }

    @app.get("/healthz")
    @app.get("/readyz")
    async def health():
        """Keep the Service endpoint reachable for existing rounds during drain."""
        await status()
        return {"healthy": True}

    @app.get("/v1/internal/telemetry")
    async def telemetry():
        """Report owned sessions for controller drain; scheduler gauges remain unknown."""
        observation = await status()
        histogram = {"count": 0, "sum_seconds": 0.0, "buckets": []}
        return {
            "version": 2,
            "collected_at_unix_ms": time.time_ns() // 1_000_000,
            "accepting": observation["accepting"],
            "running_requests": observation["active_sessions"],
            "max_concurrent_requests": None,
            "scheduler_running_requests": None,
            "scheduler_waiting_requests": None,
            "kv_cache_usage": None,
            "prompt_tokens_total": None,
            "generation_tokens_total": None,
            "ttft_seconds": histogram,
            "tpot_seconds": histogram,
            "e2e_seconds": histogram,
        }

    @app.post("/v1/internal/admission/close")
    async def close_admission():
        """Close new bindings before controller route withdrawal and session drain."""
        app.state.service.accepting = False
        return await telemetry()

    @app.post("/drain")
    async def drain():
        """Stop new bindings while keeping all existing session operations usable."""
        app.state.service.accepting = False
        return await status()

    @app.post("/sessions")
    async def open_draft(request: OpenDraft):
        """Bind a Draft context to this response stream's lifetime."""
        service = app.state.service
        service.require_role("draft")
        service.validate_tokens(request.token_ids)
        service.require_accepting()
        return StreamingResponse(
            service.open_draft(request), media_type="application/x-ndjson"
        )

    @app.post("/sessions/{session_id}/propose")
    async def propose(session_id: str, request: Propose):
        """Return one candidate chain; the owning session stream controls abort."""
        return await app.state.service.propose(session_id, request)

    @app.post("/sessions/{session_id}/commit")
    async def commit(session_id: str, request: Commit):
        """Apply the exact Target delta before accepting the next Draft round."""
        session = app.state.service.draft_session(session_id)
        app.state.service.validate_tokens(request.token_ids)
        if (
            session.active_request is not None
            or request.base_version != session.version
            or request.version <= session.version
        ):
            raise HTTPException(409, "context changed or proposal is in flight")
        session.token_ids.extend(request.token_ids)
        session.version = request.version
        return {"version": session.version}

    @app.post("/generate")
    async def generate(request: Generate):
        """Open a Target request and stream its committed output and next ticket."""
        service = app.state.service
        service.require_role("target")
        service.validate_tokens(request.token_ids)
        service.validate_tokens(request.stop_token_ids)
        if request.min_tokens > request.max_tokens:
            raise HTTPException(422, "min_tokens exceeds max_tokens")
        service.require_accepting()
        return StreamingResponse(
            service.generate(request), media_type="application/x-ndjson"
        )

    @app.post("/sessions/{session_id}/verify")
    async def verify(session_id: str, request: Verify):
        """Admit candidates for an existing Target, also while draining."""
        return await app.state.service.verify(session_id, request)

    @app.delete("/sessions/{session_id}")
    async def close(session_id: str):
        """Idempotently close a session and abort its active engine request."""
        await app.state.service.close_session(session_id)
        return {"closed": True}

    return app
