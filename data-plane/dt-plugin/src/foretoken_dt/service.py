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


class ContextSource(Message):
    """Location of the Target session holding a confirmed prefix's features."""

    endpoint: str
    session_id: str


class Propose(Message):
    """One candidate chain from an already confirmed Draft context."""

    version: StrictInt = Field(ge=0)
    max_tokens: StrictInt = Field(gt=0)
    context_source: ContextSource | None = None


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
    dtype: Literal["float32", "uint8", "float16", "bfloat16"]
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
    """Handle to claim before obtaining a Draft distribution descriptor."""

    artifact_id: str


class KvLayer(Message):
    """Physical per-layer view needed to validate a received checkpoint."""

    layer: str
    shape: list[StrictInt]
    dtype: str


class KvCheckpoint(Message):
    """Unclaimed snapshot of a paused Target's confirmed prefix and computed KV."""

    artifact_id: str
    token_ids: list[StrictInt]
    computed_tokens: StrictInt = Field(gt=0)
    layout: list[KvLayer]


class ResumeTarget(Message):
    """Resume the same confirmed prefix on a selected Target using a source checkpoint."""

    generation: Generate
    source_endpoint: str
    checkpoint: KvCheckpoint


class ExportCheckpoint(Message):
    """Export only the paused generation observed by the frontend."""

    version: StrictInt = Field(ge=0)


class ReleaseArtifact(Message):
    """Proof that the consumer will no longer read one source publication."""

    publication_id: str


class Verify(Message):
    """Ready candidates for the Target's currently published round ticket."""

    version: StrictInt = Field(ge=0)
    token_ids: list[StrictInt]
    artifact: ProposalArtifact | None = None
    source_endpoint: str | None = None


class BatchVerifyItem(Verify):
    """One independently cancellable member of a frontend verification batch."""

    session_id: str


class BatchVerify(Message):
    """Ready round members submitted together to one selected Target worker."""

    members: list[BatchVerifyItem] = Field(min_length=1)


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
        model_metadata: dict | None = None,
    ):
        self.engine = engine
        self.role = role
        self.budget = budget
        self.rdma = rdma
        self.transfers: set[asyncio.Task] = set()
        self.target_artifacts: dict[str, str] = {}
        self.verifying: set[str] = set()
        self.transfer_observations: dict[str, dict] = {}
        self.target_exports: dict[str, set[str]] = {}
        self.migration_enabled = engine.vllm_config.kv_transfer_config is not None
        self.accepting = True
        self.drafts: dict[str, DraftSession] = {}
        self.targets: dict[str, ExternalDraftRequest | None] = {}
        self.context_profile = None
        if engine.vllm_config.additional_config.get("foretoken_dt_context_role"):
            spec = engine.vllm_config.speculative_config
            self.budget = spec.num_speculative_tokens
            identity = model_metadata or {}
            self.context_profile = {
                "method": spec.method,
                "target": identity.get("model", engine.model_config.model),
                "target_revision": identity.get(
                    "revision", engine.model_config.revision
                ),
                "draft": spec.draft_model_config.model,
                "draft_revision": spec.draft_model_config.revision,
            }

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
                for artifact_id in self.target_exports.pop(session_id, set()):
                    await self.worker("dt_release", artifact_id)
                self.transfer_observations.pop(session_id, None)
                await self.engine.abort(session_id)
                artifact_id = self.target_artifacts.pop(session_id, None)
                if artifact_id is not None:
                    await self.worker("dt_release", artifact_id)

    async def open_draft(self, request: OpenDraft):
        """Keep the owning control stream open; disconnect closes the Draft session."""
        # Decide admission when the body starts, before creating any session. Drain
        # can run after HTTP headers are sent, so rejection is an explicit stream event.
        if not self.accepting:
            yield _event({"event": "admission_closed"})
            return
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

        Each round is a normal vLLM request. Eligible methods reuse immutable
        prefix blocks; rejected suffixes never enter the next round's prompt.
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
        artifact = None
        context_task = None
        try:
            prompt = list(session.token_ids)
            extra = {"dt_artifact_id": request_id} if self.rdma else {}
            if self.context_profile is not None:
                if request.context_source is None:
                    raise HTTPException(422, "neural Draft requires Target context")
                context_task = asyncio.create_task(
                    self.receive_context(session, request)
                )
                self.transfers.add(context_task)
                context_task.add_done_callback(self._transfer_finished)
                context_id = await asyncio.shield(context_task)
                if session.closed.is_set():
                    raise HTTPException(
                        409, "Draft session closed during context transfer"
                    )
                extra.update(
                    dt_context_artifact=context_id, dt_anchor_token=prompt.pop()
                )
            sampling = session.sampling.parameters()
            # Independent engines may share the same default global RNG seed.
            # Draw each proposal seed independently of Target's acceptance RNG.
            sampling["seed"] = randbits(63)
            params = SamplingParams(
                **sampling,
                extra_args=extra or None,
                max_tokens=request.max_tokens,
                ignore_eos=True,
                output_kind=RequestOutputKind.FINAL_ONLY,
            )
            tokens = None
            async with aclosing(
                self.engine.generate(
                    TokensPrompt(prompt_token_ids=prompt),
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
            if self.rdma:
                while True:
                    publication = await self.worker("dt_publish", request_id)
                    if publication["ready"]:
                        break
                    await asyncio.sleep(0.001)
                artifact = {"artifact_id": request_id}
            return {
                "version": session.version,
                "token_ids": tokens,
                "artifact": artifact,
                "observation": output.dt_observation,
            }
        finally:
            with CancelScope(shield=True):
                await self.engine.abort(request_id)
                if context_task is not None:
                    (context_id,) = await asyncio.gather(
                        context_task, return_exceptions=True
                    )
                    if isinstance(context_id, str):
                        await self.worker("dt_release", context_id)
                session.active_request = None
                if self.rdma and (artifact is None or session.closed.is_set()):
                    await self.worker("dt_release", request_id)
                    session.artifacts.discard(request_id)

    async def receive_context(self, session: DraftSession, request: Propose) -> str:
        """Pull Target features before native Draft admission; the proposal owns their release."""
        source = request.context_source
        async with httpx.AsyncClient(timeout=None) as client:
            response = await client.post(
                f"{source.endpoint.rstrip('/')}/sessions/{source.session_id}/context",
                json={"version": request.version},
            )
            response.raise_for_status()
            context = response.json()
        artifact_id = context["artifact_id"]

        async def validate(payload):
            if (
                context["profile"] != self.context_profile
                or context["token_ids"] != session.token_ids
            ):
                raise HTTPException(
                    422, "Target context profile or confirmed tokens differ"
                )
            if (
                len(payload.shape) != 3
                or payload.shape[0] != len(session.token_ids) - 1
            ):
                raise HTTPException(
                    422, "Target context does not cover the computed prefix"
                )

        result = await self.read_artifact(artifact_id, source.endpoint, validate)
        if result is None:
            raise HTTPException(409, "Target context was revoked")
        return artifact_id

    async def export_context(self, session_id, ticket, artifact_id):
        """Publish a scheduler-selected snapshot while retaining session cancellation ownership."""
        try:
            context = await self.engine.export_context(ticket, artifact_id)
            if context is None:
                raise HTTPException(409, "Target generation changed")
            while not (await self.worker("dt_publish", artifact_id))["ready"]:
                await asyncio.sleep(0.001)
            return {**context, "profile": self.context_profile}
        finally:
            if session_id not in self.targets:
                await self.worker("dt_release", artifact_id)

    async def generate(self, request: Generate, resume: ResumeTarget | None = None):
        """Stream Target-confirmed deltas; disconnect aborts the original request."""
        from vllm import SamplingParams
        from vllm.inputs import TokensPrompt
        from vllm.sampling_params import RequestOutputKind

        # Refusal is explicit and precedes session creation, prefill and KV import.
        if not self.accepting:
            yield _event({"event": "admission_closed"})
            return
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
            if resume is not None:
                artifact_id = resume.checkpoint.artifact_id
                task = asyncio.create_task(self.receive_checkpoint(session_id, resume))
                self.transfers.add(task)
                task.add_done_callback(self._transfer_finished)
                await asyncio.shield(task)
                params.extra_args = {
                    "kv_transfer_params": {
                        "artifact_id": artifact_id,
                        "computed_tokens": resume.checkpoint.computed_tokens,
                    }
                }
            # Explicit session deletion can finish while a shielded KV read is
            # still completing. Do not admit model work for that closed owner.
            if session_id not in self.targets:
                return
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
                            "observation": output.dt_observation,
                            "transfer": self.transfer_observations.pop(
                                session_id, None
                            ),
                        }
                    )
        finally:
            await self.close_session(session_id)

    async def export_checkpoint(self, session_id: str, ticket, artifact_id: str):
        """Retain an exported copy with its source session, even if the control call closes."""
        try:
            checkpoint = await self.engine.export_kv(ticket, artifact_id)
            if checkpoint is None:
                raise HTTPException(
                    409, "Target generation no longer has exportable KV"
                )
            while not (await self.worker("dt_publish", artifact_id))["ready"]:
                await asyncio.sleep(0.001)
            return checkpoint
        finally:
            if session_id not in self.targets:
                await self.worker("dt_release", artifact_id)

    async def receive_checkpoint(self, session_id: str, resume: ResumeTarget):
        """Own checkpoint claim/read/ACK and retain the receiver through its first forward."""
        checkpoint = resume.checkpoint
        artifact_id = checkpoint.artifact_id
        retained = False

        async def validate(payload):
            if payload.dtype != "uint8" or payload.shape != [payload.nbytes]:
                raise HTTPException(422, "checkpoint must be a byte tensor")
            await self.worker(
                "dt_validate_kv",
                [x.model_dump() for x in checkpoint.layout],
                payload.nbytes,
                checkpoint.computed_tokens,
            )

        started = time.monotonic()
        try:
            result = await self.read_artifact(
                artifact_id, resume.source_endpoint, validate
            )
            if result is None:
                raise HTTPException(409, "source checkpoint was revoked")
            if session_id not in self.targets:
                return
            self.target_artifacts[session_id] = artifact_id
            self.transfer_observations[session_id] = {
                "bytes": result["bytes"],
                "read_seconds": result["read_seconds"],
                "prepare_seconds": time.monotonic() - started,
            }
            retained = True
        finally:
            if not retained:
                await self.worker("dt_release", artifact_id)

    async def verify_batch(self, request: BatchVerify) -> dict:
        """Prepare all live members, then release them in one EngineCore utility call.

        Session streams retain cancellation ownership. Cancelling one member does
        not cancel sibling reads; the engine rejects that member's stale ticket.
        """
        self.require_role("target")
        ids = [member.session_id for member in request.members]
        if len(set(ids)) != len(ids):
            raise HTTPException(422, "duplicate Target session in batch")
        entries = []
        for member in request.members:
            if len(member.token_ids) > self.budget:
                raise HTTPException(422, "candidate exceeds the role's token budget")
            self.validate_tokens(member.token_ids)
            needs_tensor = self.rdma and bool(member.token_ids)
            if needs_tensor:
                if member.artifact is None or member.source_endpoint is None:
                    raise HTTPException(
                        422, "RDMA verification requires a Draft publication"
                    )
            elif member.artifact is not None:
                raise HTTPException(422, "this verification does not consume a tensor")
            if member.session_id in self.verifying:
                raise HTTPException(409, "verification is already in flight")
            ticket = self.targets.get(member.session_id)
            entries.append(
                ticket
                if ticket is not None and ticket.generation == member.version
                else None
            )
        self.verifying.update(ids)
        # A control disconnect cannot abandon a claimed source or an in-flight read.
        task = asyncio.create_task(
            self.prepare_and_submit_batch(request.members, entries)
        )
        self.transfers.add(task)
        task.add_done_callback(self._transfer_finished)
        return await asyncio.shield(task)

    async def prepare_and_submit_batch(self, members, tickets) -> dict:
        """Own transfer completion and cleanup until batch admission is acknowledged."""
        accepted = [False] * len(members)
        submission_started = False
        admission_returned = False
        try:

            async def prepare(member, ticket):
                if ticket is None:
                    return False
                if self.rdma and member.token_ids:
                    return await self.receive_and_stage(
                        member.session_id, ticket, member
                    )
                return True

            # Observe every read before raising an error: one failed member must not
            # abandon a sibling's DMA or release its source prematurely.
            prepared = await asyncio.gather(
                *(prepare(member, ticket) for member, ticket in zip(members, tickets)),
                return_exceptions=True,
            )
            for result in prepared:
                if isinstance(result, BaseException):
                    raise result
            indexes = [i for i, ready in enumerate(prepared) if ready]
            if indexes:
                submission_started = True
                results = await self.engine.submit_external_draft_batch(
                    [(tickets[i], members[i].token_ids) for i in indexes]
                )
                for index, result in zip(indexes, results, strict=True):
                    accepted[index] = result
                admission_returned = True
            return {"accepted": accepted}
        finally:
            for member, admitted in zip(members, accepted):
                self.verifying.discard(member.session_id)
                # A failed utility response does not prove the engine rejected
                # admission. Keep staged storage with the session until output
                # or abort establishes that the engine can no longer consume it.
                if not admitted and (not submission_started or admission_returned):
                    self.transfer_observations.pop(member.session_id, None)
                    artifact_id = self.target_artifacts.pop(member.session_id, None)
                    if artifact_id is not None:
                        await self.worker("dt_release", artifact_id)

    def _transfer_finished(self, task: asyncio.Task) -> None:
        """Observe failures even when the initiating HTTP request disconnected."""
        self.transfers.discard(task)
        if not task.cancelled():
            error = task.exception()
            if error is not None:
                logger.error(
                    "DT transfer failed",
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
                and status["retained_artifacts"]
                == status["unacknowledged_publications"]
            ):
                # Without a remote ACK, process termination is the safe release
                # boundary; never unregister these potentially readable buffers.
                logger.warning(
                    "Terminating Worker with %d unacknowledged DT publications",
                    status["unacknowledged_publications"],
                )
                return
            await asyncio.sleep(0.001)

    async def read_artifact(self, artifact_id, source_endpoint, validate):
        """Own one claim/read/ACK; failed reads retain storage until DMA is known complete."""
        read_started = False
        read_finished = False
        try:
            async with httpx.AsyncClient() as client:
                source = f"{source_endpoint.rstrip('/')}/artifacts/{artifact_id}"
                response = await client.post(f"{source}/claim")
                if response.status_code == 409:
                    return None
                response.raise_for_status()
                payload = TensorPayload.model_validate(response.json())
                try:
                    await validate(payload)
                    while True:
                        read_started = True
                        result = await self.worker(
                            "dt_receive", artifact_id, payload.descriptor()
                        )
                        if result["ready"]:
                            read_finished = True
                            return result
                        await asyncio.sleep(0.001)
                finally:
                    if not read_started or read_finished:
                        response = await client.post(
                            f"{source}/release",
                            json={"publication_id": payload.publication_id},
                        )
                        response.raise_for_status()
        except BaseException:
            # An uncertain read stays registered: dt_release awaits its operation.
            if read_started:
                await self.worker("dt_release", artifact_id)
            raise

    async def receive_and_stage(self, session_id, ticket, request: Verify) -> bool:
        """Receive one proposal, then stage it only for the still-live Target ticket."""
        started = time.monotonic()
        artifact_id = request.artifact.artifact_id
        staged = False

        async def validate(payload):
            expected = [
                len(request.token_ids),
                self.engine.model_config.get_vocab_size(),
            ]
            if payload.dtype != "float32" or payload.shape != expected:
                raise HTTPException(
                    422, "Draft distribution does not match candidates and vocabulary"
                )

        try:
            result = await self.read_artifact(
                artifact_id, request.source_endpoint, validate
            )
            if result is None or self.targets.get(session_id) != ticket:
                return False
            await self.worker(
                "dt_stage", artifact_id, ticket.request_id, ticket.generation
            )
            if self.targets.get(session_id) != ticket:
                return False
            self.target_artifacts[session_id] = artifact_id
            self.transfer_observations[session_id] = {
                "bytes": result["bytes"],
                "read_seconds": result["read_seconds"],
                "prepare_seconds": time.monotonic() - started,
            }
            staged = True
            return True
        finally:
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
        service = RoleService(engine, role, budget, rdma_host is not None, metadata)
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
    async def status(observations: bool = True):
        """Report role identity; registry probes omit observations that wait on EngineCore."""
        from vllm.v1.engine.exceptions import EngineDeadError

        service = app.state.service
        try:
            await service.engine.check_health()
        except EngineDeadError as error:
            raise HTTPException(503, "role engine is unavailable") from error
        result = {
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
            "token_budget": service.budget,
            "kv_migration": service.migration_enabled,
            "context_profile": service.context_profile,
        }
        if observations:
            transport = await service.worker("dt_status") if service.rdma else None
            result["retained_artifacts"] = (
                transport["retained_artifacts"] if transport else 0
            )
            result["workload"] = await service.engine.workload()
        return result

    @app.get("/healthz")
    @app.get("/readyz")
    async def health():
        """Keep the Service endpoint reachable for existing rounds during drain."""
        await status(observations=False)
        return {"healthy": True}

    @app.get("/v1/internal/telemetry")
    async def telemetry():
        """Preserve session drain counts and publish native scheduler gauges to Router."""
        observation = await status()
        workload = observation["workload"]
        histogram = {"count": 0, "sum_seconds": 0.0, "buckets": []}
        return {
            "version": 2,
            "collected_at_unix_ms": workload["collected_at_unix_ms"],
            "draft_target_workload": workload,
            "accepting": observation["accepting"],
            "running_requests": observation["active_sessions"],
            "max_concurrent_requests": workload["max_num_seqs"],
            "scheduler_running_requests": workload["running_requests"],
            "scheduler_waiting_requests": workload["waiting_requests"],
            "kv_cache_usage": workload["kv_cache_usage"],
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
        return StreamingResponse(
            service.open_draft(request), media_type="application/x-ndjson"
        )

    @app.get("/sessions/{session_id}/kv")
    async def request_kv(session_id: str):
        """Read current session residency for a placement decision; this is not a KV lease."""
        service = app.state.service
        if service.role == "target":
            if session_id not in service.targets:
                raise HTTPException(404, "target session is closed or unknown")
            request_id = session_id
        else:
            request_id = service.draft_session(session_id).active_request
        return {
            "collected_at_unix_ms": time.time_ns() // 1_000_000,
            "kv": await service.engine.request_kv(request_id) if request_id else None,
        }

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

    def validate_generation(request: Generate):
        """Validate both fresh and migrated Target admission at their shared boundary."""
        service = app.state.service
        service.require_role("target")
        service.validate_tokens(request.token_ids)
        service.validate_tokens(request.stop_token_ids)
        service.validate_sampling(request.sampling)
        if request.min_tokens > request.max_tokens:
            raise HTTPException(422, "min_tokens exceeds max_tokens")

    @app.post("/generate")
    async def generate(request: Generate):
        """Open a Target request and stream its committed output and next ticket."""
        validate_generation(request)
        return StreamingResponse(
            app.state.service.generate(request), media_type="application/x-ndjson"
        )

    @app.post("/sessions/{session_id}/checkpoint")
    async def checkpoint(session_id: str, request: ExportCheckpoint):
        """Export a paused Target generation; its session owns the unclaimed snapshot."""
        service = app.state.service
        service.require_role("target")
        if not service.migration_enabled:
            raise HTTPException(422, "Target KV migration is disabled")
        ticket = service.targets.get(session_id)
        if ticket is None or ticket.generation != request.version:
            raise HTTPException(409, "Target generation changed")
        artifact_id = uuid4().hex
        service.target_exports.setdefault(session_id, set()).add(artifact_id)
        task = asyncio.create_task(
            service.export_checkpoint(session_id, ticket, artifact_id)
        )
        service.transfers.add(task)
        task.add_done_callback(service._transfer_finished)
        return await asyncio.shield(task)

    @app.post("/sessions/{session_id}/context")
    async def context(session_id: str, request: ExportCheckpoint):
        """Export the confirmed feature prefix for a neural Draft proposal."""
        service = app.state.service
        service.require_role("target")
        if service.context_profile is None:
            raise HTTPException(422, "Target feature export is disabled")
        ticket = service.targets.get(session_id)
        if ticket is None or ticket.generation != request.version:
            raise HTTPException(409, "Target generation changed")
        artifact_id = uuid4().hex
        service.target_exports.setdefault(session_id, set()).add(artifact_id)
        task = asyncio.create_task(
            service.export_context(session_id, ticket, artifact_id)
        )
        service.transfers.add(task)
        task.add_done_callback(service._transfer_finished)
        return await asyncio.shield(task)

    @app.post("/resume")
    async def resume(request: ResumeTarget):
        """Receive KV before admitting generation from the same confirmed prefix."""
        service = app.state.service
        validate_generation(request.generation)
        if (
            not service.migration_enabled
            or request.generation.sampling.temperature != 0
            or request.generation.stop
            or request.checkpoint.token_ids != request.generation.token_ids
            or request.checkpoint.computed_tokens >= len(request.generation.token_ids)
        ):
            raise HTTPException(
                422,
                "resume requires enabled migration, greedy sampling and matching KV prefix",
            )
        return StreamingResponse(
            service.generate(request.generation, request),
            media_type="application/x-ndjson",
        )

    @app.post("/sessions/{session_id}/verify")
    async def verify(session_id: str, request: Verify):
        """Admit candidates for an existing Target, also while draining."""
        service = app.state.service
        service.require_role("target")
        if session_id not in service.targets:
            raise HTTPException(404, "target session is closed or unknown")
        result = await service.verify_batch(
            BatchVerify(
                members=[BatchVerifyItem(session_id=session_id, **request.model_dump())]
            )
        )
        return {"accepted": result["accepted"][0]}

    @app.post("/batch/verify")
    async def verify_batch(request: BatchVerify):
        """Stage the round's tensors before releasing live batch members together."""
        return await app.state.service.verify_batch(request)

    @app.post("/artifacts/{artifact_id}/claim")
    async def claim_artifact(artifact_id: str):
        """Grant one Target a descriptor unless cancellation already revoked it."""
        service = app.state.service
        payload = await service.worker("dt_claim", artifact_id)
        if payload is None:
            raise HTTPException(409, "artifact is unavailable or already claimed")
        return payload

    @app.post("/artifacts/{artifact_id}/release")
    async def release_artifact(artifact_id: str, request: ReleaseArtifact):
        """Release a claimed publication after Target guarantees no further read."""
        service = app.state.service
        await service.worker("dt_release", artifact_id, request.publication_id)
        for session in service.drafts.values():
            session.artifacts.discard(artifact_id)
        for exports in service.target_exports.values():
            exports.discard(artifact_id)
        return {"released": True}

    @app.delete("/sessions/{session_id}")
    async def close(session_id: str):
        """Idempotently close a session and abort its active engine request."""
        await app.state.service.close_session(session_id)
        return {"closed": True}

    return app
