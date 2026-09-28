# SPDX-License-Identifier: Apache-2.0
# SPDX-FileCopyrightText: Copyright contributors to the Foretoken project

"""Independent role execution; placement and round orchestration belong to the frontend."""

from __future__ import annotations

import asyncio
import json
import logging
import os
import time
from contextlib import aclosing, asynccontextmanager
from dataclasses import dataclass, field
from secrets import randbits
from typing import TYPE_CHECKING, Literal
from uuid import uuid4

import httpx
from anyio import CancelScope
from fastapi import FastAPI, HTTPException
from fastapi.responses import StreamingResponse
from pydantic import BaseModel, ConfigDict, Field, StrictInt

logger = logging.getLogger(__name__)

if TYPE_CHECKING:
    from vllm import AsyncEngineArgs

    from .vllm.engine import ExternalAsyncLLM as AsyncLLM
    from .vllm.engine import ExternalDraftRequest


class Message(BaseModel):
    """Explicit internal HTTP request schema; unknown fields are rejected."""

    model_config = ConfigDict(extra="forbid")


class Sampling(Message):
    """Sampling choices shared by Draft proposals and Target verification."""

    temperature: float = Field(default=0, ge=0)
    top_p: float = Field(default=1, gt=0, le=1)
    top_k: StrictInt = Field(default=0, ge=0)
    seed: StrictInt | None = None

    def parameters(self) -> dict:
        """Map supported choices explicitly into vLLM SamplingParams."""
        return {
            "temperature": self.temperature,
            "top_p": self.top_p,
            "top_k": self.top_k or -1,
            "seed": self.seed,
        }


class OpenDraft(Message):
    """Confirmed token context supplied by the frontend when binding a Draft."""

    token_ids: list[StrictInt] = Field(min_length=1)
    version: StrictInt = Field(ge=0)
    sampling: Sampling = Field(default_factory=Sampling)


class Propose(Message):
    """One candidate chain from an already confirmed Draft context."""

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
    sampling: Sampling = Field(default_factory=Sampling)


class TensorPayload(Message):
    """Registered GPU tensor descriptor; tensor contents never enter HTTP."""

    publication_id: str
    segment: str
    address: StrictInt = Field(gt=0)
    nbytes: StrictInt = Field(gt=0)
    dtype: Literal["float32"]
    shape: list[StrictInt]

    def descriptor(self) -> dict:
        """Encode the transfer descriptor consumed by the worker RPC."""
        return {
            "publication_id": self.publication_id,
            "segment": self.segment,
            "address": self.address,
            "nbytes": self.nbytes,
            "dtype": self.dtype,
            "shape": self.shape,
        }


class ProposalArtifact(Message):
    """Identify one immutable Draft distribution publication."""

    artifact_id: str
    payload: TensorPayload


class ReleaseArtifact(Message):
    """Proof that the consumer finished reading one source publication."""

    publication_id: str


class Verify(Message):
    """Ready candidates for the Target's currently published round ticket."""

    version: StrictInt = Field(ge=0)
    token_ids: list[StrictInt]
    artifact: ProposalArtifact | None = None
    source_endpoint: str | None = None


@dataclass
class DraftSession:
    """Connection-owned confirmed prefix; vLLM owns all cached model state."""

    token_ids: list[int]
    version: int
    sampling: Sampling
    artifacts: set[str] = field(default_factory=set)
    closed: asyncio.Event = field(default_factory=asyncio.Event)
    active_request: str | None = None


def _event(value: dict) -> bytes:
    """Encode an explicit control event; no model tensors enter this protocol."""
    return (json.dumps(value) + "\n").encode()


class RoleService:
    """Own one engine and its connection-bound sessions until role shutdown."""

    def __init__(
        self,
        engine: AsyncLLM,
        role: Literal["draft", "target"],
        budget: int,
        rdma: bool = False,
    ):
        self.engine = engine
        self.role = role
        self.budget = budget
        self.rdma = rdma
        self.transfers: set[asyncio.Task] = set()
        self.target_artifacts: dict[str, str] = {}
        self.verifying: set[str] = set()
        self.accepting = True
        self.drafts: dict[str, DraftSession] = {}
        self.targets: dict[str, ExternalDraftRequest | None] = {}

    async def worker(self, method: str, *args):
        """Call the sole local worker without blocking the service event loop."""
        results = await self.engine.collective_rpc(method, args=args)
        (result,) = results
        return result

    def validate_sampling(self, sampling: Sampling) -> None:
        """Reject random proposals before admission when no tensor transport exists."""
        if not self.rdma and sampling.temperature != 0:
            raise HTTPException(422, "random sampling requires RDMA on both roles")

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
                for artifact_id in tuple(session.artifacts):
                    await self.worker("dt_release", artifact_id)
            if session_id in self.targets:
                del self.targets[session_id]
                await self.engine.abort(session_id)
                artifact_id = self.target_artifacts.pop(session_id, None)
                if artifact_id is not None:
                    await self.worker("dt_release", artifact_id)

    async def open_draft(self, request: OpenDraft):
        """Keep the owning control stream open; disconnect closes the Draft session."""
        self.require_accepting()
        session_id = uuid4().hex
        session = DraftSession(
            list(request.token_ids), request.version, request.sampling
        )
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
        if self.rdma:
            session.artifacts.add(request_id)
        try:
            sampling = session.sampling.parameters()
            # Independent engines may share the same default global RNG seed.
            # Draw each proposal seed independently of Target's acceptance RNG.
            sampling["seed"] = randbits(63)
            params = SamplingParams(
                **sampling,
                extra_args={"dt_artifact_id": request_id} if self.rdma else None,
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
            artifact = None
            if self.rdma:
                while True:
                    publication = await self.worker("dt_publish", request_id)
                    if publication["ready"]:
                        break
                    await asyncio.sleep(0.001)
                artifact = {
                    "artifact_id": request_id,
                    "payload": publication["payload"],
                }
            return {
                "version": session.version,
                "token_ids": tokens,
                "artifact": artifact,
            }
        finally:
            with CancelScope(shield=True):
                await self.engine.abort(request_id)
                session.active_request = None
                if self.rdma:
                    await self.worker("dt_release", request_id)

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
                **request.sampling.parameters(),
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
                    artifact_id = self.target_artifacts.pop(session_id, None)
                    if artifact_id is not None:
                        await self.worker("dt_release", artifact_id)
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
        if session_id in self.verifying:
            raise HTTPException(409, "verification is already in flight")
        if self.rdma and request.token_ids:
            if request.artifact is None or request.source_endpoint is None:
                raise HTTPException(
                    422, "RDMA verification requires a Draft publication"
                )
            expected = [
                len(request.token_ids),
                self.engine.model_config.get_vocab_size(),
            ]
            if request.artifact.payload.shape != expected:
                raise HTTPException(
                    422, "Draft distribution does not match candidates and vocabulary"
                )
            # A control disconnect must not abandon a live DMA read or its source ACK.
            self.verifying.add(session_id)
            task = asyncio.create_task(
                self.receive_and_verify(session_id, ticket, request)
            )
            self.transfers.add(task)
            task.add_done_callback(self._transfer_finished)
            return await asyncio.shield(task)
        if request.artifact is not None:
            raise HTTPException(422, "this verification does not consume a tensor")
        accepted = await self.engine.submit_external_draft_tokens(
            ticket, request.token_ids
        )
        return {"accepted": accepted}

    def _transfer_finished(self, task: asyncio.Task) -> None:
        """Observe failures even when the initiating HTTP request disconnected."""
        self.transfers.discard(task)
        if not task.cancelled():
            error = task.exception()
            if error is not None:
                logger.error(
                    "DT receive/verification failed",
                    exc_info=(type(error), error, error.__traceback__),
                )

    async def drain_transfers(self) -> None:
        """Finish background reads and ACKs before terminating the owning Worker."""
        if self.transfers:
            # Every failure is logged by the task callback. Wait for all owners,
            # even if one failed; one error cannot abandon another live read.
            await asyncio.gather(*tuple(self.transfers), return_exceptions=True)
        if not self.rdma:
            return
        while True:
            result = await self.worker("dt_close")
            if result["ready"]:
                return
            status = await self.worker("dt_status")
            if (
                status["retained_artifacts"] > 0
                and status["retained_artifacts"] == status["published_artifacts"]
            ):
                # Without a remote ACK, process termination is the safe release
                # boundary; never unregister these potentially readable buffers.
                logger.warning(
                    "Terminating Worker with %d unacknowledged DT publications",
                    status["published_artifacts"],
                )
                return
            await asyncio.sleep(0.001)

    async def receive_and_verify(self, session_id, ticket, request: Verify) -> dict:
        """Own a remote read through ACK; only a live ticket may consume its tensor."""
        artifact = request.artifact
        artifact_id = artifact.artifact_id
        staged = False
        try:
            while True:
                result = await self.worker(
                    "dt_receive", artifact_id, artifact.payload.descriptor()
                )
                if result["ready"]:
                    break
                await asyncio.sleep(0.001)
            async with httpx.AsyncClient() as client:
                response = await client.post(
                    f"{request.source_endpoint.rstrip('/')}/artifacts/{artifact_id}/release",
                    json={"publication_id": artifact.payload.publication_id},
                )
                response.raise_for_status()
            if self.targets.get(session_id) != ticket:
                return {"accepted": False}
            await self.worker(
                "dt_stage", artifact_id, ticket.request_id, ticket.generation
            )
            if self.targets.get(session_id) != ticket:
                return {"accepted": False}
            self.target_artifacts[session_id] = artifact_id
            staged = True
            accepted = await self.engine.submit_external_draft_tokens(
                ticket, request.token_ids
            )
            if not accepted:
                self.target_artifacts.pop(session_id, None)
                staged = False
            return {"accepted": accepted}
        finally:
            self.verifying.discard(session_id)
            if not staged:
                await self.worker("dt_release", artifact_id)


def create_app(
    engine_args: AsyncEngineArgs,
    role: Literal["draft", "target"],
    budget: int,
    rdma_host: str | None = None,
    rdma_nic: str = "",
) -> FastAPI:
    """Build an internal role API; its lifespan exclusively owns the vLLM engine."""
    from .vllm.engine import ExternalAsyncLLM as AsyncLLM

    # The managed launcher preserves provider identity when loading local snapshots.
    metadata = json.loads(os.environ.get("FORETOKEN_DT_MODEL_METADATA", "{}"))

    @asynccontextmanager
    async def lifespan(app):
        engine = AsyncLLM.from_engine_args(engine_args)
        service = RoleService(engine, role, budget, rdma_host is not None)
        app.state.service = service
        connector_initialized = False
        try:
            if rdma_host is not None:
                await service.worker("dt_initialize", role, rdma_host, rdma_nic)
                connector_initialized = True
            yield
        finally:
            service.accepting = False
            try:
                for session_id in list(service.drafts) + list(service.targets):
                    await service.close_session(session_id)
                if connector_initialized:
                    await service.drain_transfers()
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
        transport = await service.worker("dt_status") if service.rdma else None
        return {
            "role": role,
            "model": metadata.get("model", service.engine.model_config.model),
            "revision": metadata.get("revision", service.engine.model_config.revision),
            "tokenizer": metadata.get(
                "tokenizer", service.engine.model_config.tokenizer
            ),
            "tokenizer_revision": metadata.get(
                "tokenizer_revision", service.engine.model_config.tokenizer_revision
            ),
            "prepared_tokenizer": metadata.get("prepared_tokenizer"),
            "max_model_len": service.engine.model_config.max_model_len,
            "accepting": service.accepting,
            "active_sessions": len(service.drafts) + len(service.targets),
            "candidate_format": "token_ids_log_probs"
            if service.rdma
            else "greedy_token_ids",
            "token_budget": budget,
            "retained_artifacts": transport["retained_artifacts"] if transport else 0,
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
        service.validate_sampling(request.sampling)
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
        service.validate_sampling(request.sampling)
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

    @app.post("/artifacts/{artifact_id}/release")
    async def release_artifact(artifact_id: str, request: ReleaseArtifact):
        """Release a Draft publication only after its reader confirms completion."""
        service = app.state.service
        service.require_role("draft")
        await service.worker("dt_release", artifact_id, request.publication_id)
        for session in service.drafts.values():
            session.artifacts.discard(artifact_id)
        return {"released": True}

    @app.delete("/sessions/{session_id}")
    async def close(session_id: str):
        """Idempotently close a session and abort its active engine request."""
        await app.state.service.close_session(session_id)
        return {"closed": True}

    return app
