# SPDX-License-Identifier: Apache-2.0
# SPDX-FileCopyrightText: Copyright contributors to the Foretoken project

"""Target-owned request rounds and connection-owned native engine sessions."""

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

from anyio import CancelScope
from fastapi import FastAPI, HTTPException
from fastapi.responses import StreamingResponse
from pydantic import BaseModel, ConfigDict, Field, StrictInt

from .connections import RdmaIdentity, RoleCommunication
from .observations import RpcTiming

logger = logging.getLogger(__name__)

ROLE_PROTOCOL_VERSION = 14
CONTEXT_REVOKED = "Target context was revoked"

if TYPE_CHECKING:
    from vllm import AsyncEngineArgs

    from .coordinator import TargetCoordinator
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
    """Confirmed token context supplied by Target or a manual diagnostic caller."""

    token_ids: list[StrictInt] = Field(min_length=1)
    version: StrictInt = Field(ge=0)
    sampling: Sampling = Field(default_factory=Sampling)
    priority: StrictInt = 0
    tensor_transport: Literal["gpu_ipc", "rdma", "zmq"] | None = None


class ContextReference(Message):
    """Prepared feature interval; claim grants its sole reader the physical descriptor."""

    artifact_id: str
    token_ids: list[StrictInt]
    start_token: StrictInt = Field(ge=0)
    end_token: StrictInt = Field(gt=0)
    profile: dict
    export_seconds: float = Field(ge=0)


class ContextSource(Message):
    """Bound Target session and optional proactively prepared feature interval."""

    endpoint: str
    session_id: str
    context: ContextReference | None = None


class Commit(Message):
    """Target-confirmed delta, never a Draft's guess at the accepted prefix."""

    base_version: StrictInt = Field(ge=0)
    version: StrictInt = Field(ge=0)
    token_ids: list[StrictInt] = Field(min_length=1)


class Propose(Message):
    """One candidate chain, optionally applying the exact confirmed delta in the same operation."""

    version: StrictInt = Field(ge=0)
    max_tokens: StrictInt = Field(gt=0)
    context_source: ContextSource | None = None
    work: dict | None = None
    confirmed_delta: Commit | None = None


class Generate(Message):
    """Token-input Target generation; final stopping remains owned by vLLM."""

    token_ids: list[StrictInt] = Field(min_length=1)
    max_tokens: StrictInt = Field(gt=0)
    stop: list[str] = Field(default_factory=list)
    stop_token_ids: list[StrictInt] = Field(default_factory=list)
    min_tokens: StrictInt = Field(default=0, ge=0)
    ignore_eos: bool = False
    sampling: Sampling = Field(default_factory=Sampling)
    priority: StrictInt = 0
    work: dict | None = None
    request_id: str | None = None
    draft_endpoints: list[str] = Field(default_factory=list)
    target_endpoint: str | None = None


class RpcObservation(Message):
    """Local Worker RPC accounting attached to successful source control replies."""

    calls: StrictInt
    dispatch_seconds: float
    worker_seconds: float
    return_seconds: float


class TensorPayload(Message):
    """Worker tensor descriptor; physical tensor contents never enter HTTP."""

    publication_id: str
    segment: str
    address: StrictInt | None = Field(default=None, gt=0)
    transport: Literal["gpu_ipc", "rdma", "zmq"]
    nbytes: StrictInt = Field(gt=0)
    dtype: Literal["float32", "uint8", "float16", "bfloat16"]
    shape: list[StrictInt]
    claim_rpc: RpcObservation | None = None

    def descriptor(self) -> dict:
        """Encode the transfer descriptor consumed by the worker RPC."""
        return {
            "publication_id": self.publication_id,
            "segment": self.segment,
            "address": self.address,
            "nbytes": self.nbytes,
            "dtype": self.dtype,
            "shape": self.shape,
            "transport": self.transport,
        }


class ProposalArtifact(Message):
    """Handle to claim before obtaining a Draft distribution descriptor."""

    artifact_id: str


class TransportPeer(Message):
    """Address the actual peer incarnations; HTTP URLs alone cannot identify GPU connections."""

    identity: RdmaIdentity
    peer: RdmaIdentity
    peer_endpoint: str


class ReadTransportProbe(TransportPeer):
    """Read a connection-owned probe through the ordinary artifact lifecycle."""

    artifact_id: str


class ProfileOperation(Message):
    """Private native capture command; the model-server supervisor owns its lifecycle."""

    start: bool
    directory: str | None = None
    profile_prefix: str | None = None


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


class ExportContext(Message):
    """Request features over the path selected for this Draft/Target binding."""

    version: StrictInt = Field(ge=0)
    tensor_transport: Literal["gpu_ipc", "rdma", "zmq"]
    start_token: StrictInt = Field(ge=0)


class ReleaseArtifact(Message):
    """Proof that the consumer will no longer read one source publication."""

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
    priority: int
    tensor_transport: str | None
    artifacts: set[str] = field(default_factory=set)
    closed: asyncio.Event = field(default_factory=asyncio.Event)
    proposing: bool = False
    context_source: ContextSource | None = None
    context_tokens: int = 0
    proposed_version: int | None = None


def _event(value: dict) -> bytes:
    """Encode an explicit control event; no model tensors enter this protocol."""
    return (json.dumps(value) + "\n").encode()


class RoleService:
    """Own one engine and its connection-bound sessions until role shutdown."""

    def __init__(
        self,
        engine: AsyncLLM,
        role: Literal["draft", "target"],
        default_draft_tokens: int | None,
        max_candidate_tokens: int | None,
        tensor_transports: tuple[Literal["gpu_ipc", "rdma", "zmq"], ...] = (),
        model_metadata: dict | None = None,
    ):
        """Retain role policy and native limits; create_app owns final engine shutdown."""
        self.engine = engine
        self.model_metadata = model_metadata or {}
        self.coordinators: dict[str, TargetCoordinator] = {}
        self.session_tasks: dict[str, set[asyncio.Task]] = {}
        # Completed failures stay owned until the next round or close observes
        # them; successful independent reclamation leaves no request history.
        self.reclamations: dict[str, set[asyncio.Task]] = {}
        self.verifying: set[str] = set()
        self.role = role
        self.default_draft_tokens = default_draft_tokens
        self.max_candidate_tokens = max_candidate_tokens
        self.tensor_transports = tensor_transports
        self.gpu_ipc: dict | None = None
        self.communication = RoleCommunication(self)
        self.operations: set[asyncio.Task] = set()
        self.target_artifacts: dict[str, str] = {}
        self.transfer_observations: dict[str, dict] = {}
        self.target_exports: dict[str, dict[str, asyncio.Task | None]] = {}
        self.migration_enabled = engine.vllm_config.kv_transfer_config is not None
        self.accepting = True
        self.drafts: dict[str, DraftSession] = {}
        self.closing: dict[str, asyncio.Task[None]] = {}
        self.targets: dict[str, ExternalDraftRequest | None] = {}
        self.context_profile = None
        if engine.vllm_config.additional_config.get("foretoken_dt_context_role"):
            spec = engine.vllm_config.speculative_config
            identity = model_metadata or {}
            self.context_profile = {
                "method": spec.method,
                "target": identity.get("model", engine.model_config.model),
                "target_revision": identity.get("resolved_revision")
                or getattr(engine.model_config.hf_config, "_commit_hash", None)
                or engine.model_config.revision,
                "draft": identity.get("draft_model", spec.draft_model_config.model),
                "draft_revision": identity.get("resolved_draft_revision")
                or getattr(spec.draft_model_config.hf_config, "_commit_hash", None)
                or spec.draft_model_config.revision,
            }

    async def worker(self, method: str, *args, timing: RpcTiming | None = None):
        """Call the local Worker, optionally splitting its existing RPC's queue and execution."""
        sent = time.monotonic()
        result = await self.engine.worker_rpc(
            method if timing is None else "dt_timed_rpc",
            args if timing is None else (method, args),
        )
        if timing is not None:
            timing.record(sent, time.monotonic(), result["started"], result["finished"])
            return result["value"]
        return result

    async def release_artifact(
        self,
        artifact_id: str,
        publication_id: str | None = None,
        *,
        timing: RpcTiming | None = None,
    ) -> None:
        """Join Worker release, including DMA/GPU fencing and any outstanding source ACK."""
        while not await self.worker(
            "dt_release", artifact_id, publication_id, timing=timing
        ):
            await asyncio.sleep(0.001)

    def validate_sampling(self, sampling: Sampling) -> None:
        """Reject random proposals before admission when no tensor transport exists."""
        if not self.tensor_transports and sampling.temperature != 0:
            raise HTTPException(
                422, "random sampling requires tensor transport on both roles"
            )

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
        self.check_reclamations(session_id)
        return session

    def validate_tokens(self, tokens: list[int]) -> None:
        """Reject out-of-vocabulary control input before mutating session state."""
        vocab_size = self.engine.model_config.get_vocab_size()
        if any(token < 0 or token >= vocab_size for token in tokens):
            raise HTTPException(422, "token IDs are outside this model's vocabulary")

    async def close_session(self, session_id: str) -> None:
        """Join the session's single close owner, including concurrent DELETE and stream cleanup."""
        with CancelScope(shield=True):
            task = self.closing.get(session_id)
            if task is None:
                task = asyncio.create_task(self._close_session(session_id))
                self.closing[session_id] = task
            try:
                await asyncio.shield(task)
            finally:
                # A failed close must remain failed for every later DELETE or stream cleanup.
                if task.done() and not task.cancelled() and task.exception() is None:
                    self.closing.pop(session_id, None)

    async def _close_session(self, session_id: str) -> None:
        """Revoke one session and acknowledge native execution and artifact cleanup once."""
        session = self.drafts.pop(session_id, None)
        if session is not None:
            session.closed.set()
            try:
                await self.engine.abort(session_id)
            finally:
                await asyncio.gather(
                    *tuple(self.session_tasks.get(session_id, ())),
                    return_exceptions=True,
                )
            await self.engine.forget_draft(session_id)
            if self.context_profile is not None:
                await self.worker("dt_forget_draft_context", session_id)
            for artifact_id in tuple(session.artifacts):
                await self.release_artifact(artifact_id)
            self.transfer_observations.pop(session_id, None)
        if session_id in self.targets:
            del self.targets[session_id]
            coordinator = self.coordinators.pop(session_id, None)
            peer_close = (
                asyncio.create_task(coordinator.peer.close()) if coordinator else None
            )
            try:
                await self.engine.abort(session_id)
            finally:
                # Started reads keep their completion/ACK responsibility even when
                # native abort fails. Join both peers before releasing storage.
                operations = tuple(self.session_tasks.get(session_id, ()))
                if peer_close is not None:
                    operations += (peer_close,)
                await asyncio.gather(*operations, return_exceptions=True)
            exports = self.target_exports.get(session_id, {})
            for artifact_id, preparation in tuple(exports.items()):
                publication_id = None
                if preparation is not None:
                    # No claim exposed this reserved descriptor to a remote reader.
                    # Join preparation before using that proof to reclaim its buffer.
                    try:
                        context = await asyncio.shield(preparation)
                    except HTTPException as error:
                        if error.status_code != 409:
                            raise
                    else:
                        publication_id = context["payload"]["publication_id"]
                await self.release_artifact(artifact_id, publication_id)
                exports.pop(artifact_id, None)
            self.target_exports.pop(session_id, None)
            self.transfer_observations.pop(session_id, None)
            artifact_id = self.target_artifacts.get(session_id)
            if artifact_id is not None:
                await self.release_artifact(artifact_id)
                self.target_artifacts.pop(session_id, None)
            if peer_close is not None:
                peer_close.result()
        reclamations = self.reclamations.pop(session_id, set())
        if reclamations:
            results = await asyncio.gather(*reclamations, return_exceptions=True)
            for result in results:
                if isinstance(result, BaseException):
                    raise result

    def defer_reclamation(self, session_id: str, operation) -> asyncio.Task:
        """Retain independent cleanup under the existing session operation owner."""
        task = self.start_operation(session_id, operation)
        owned = self.reclamations.setdefault(session_id, set())
        owned.add(task)

        def completed(task):
            if not task.cancelled() and task.exception() is None:
                owned.discard(task)
                if not owned and self.reclamations.get(session_id) is owned:
                    self.reclamations.pop(session_id, None)

        task.add_done_callback(completed)
        return task

    def check_reclamations(self, session_id: str) -> None:
        """Surface completed cleanup failures before another round can advance."""
        for task in self.reclamations.get(session_id, ()):
            if task.done():
                task.result()

    async def wait_ready(self, session_id: str, task, ready):
        """Return member readiness while its retained operation joins ACK and cleanup."""
        done, _ = await asyncio.wait((task, ready), return_when=asyncio.FIRST_COMPLETED)
        if task in done:
            # An error before readiness is still this caller's request error,
            # rather than a detached cleanup failure during a later close.
            owned = self.reclamations.get(session_id)
            if owned is not None:
                owned.discard(task)
                if not owned:
                    self.reclamations.pop(session_id, None)
            return task.result()
        return ready.result()

    def start_operation(self, session_id: str, operation) -> asyncio.Task:
        """Retain one session's asynchronous operation until completion and safe cleanup."""
        task = asyncio.create_task(operation)
        self.operations.add(task)
        owned = self.session_tasks.setdefault(session_id, set())
        owned.add(task)

        def completed(task):
            owned.discard(task)
            if not owned:
                self.session_tasks.pop(session_id, None)
            self._operation_finished(task)

        task.add_done_callback(completed)
        return task

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
            list(request.token_ids),
            request.version,
            request.sampling,
            request.priority,
            request.tensor_transport,
        )
        self.drafts[session_id] = session
        try:
            yield _event(
                {
                    "event": "opened",
                    "session_id": session_id,
                    "rdma": self.communication.identity.model_dump()
                    if self.communication.identity
                    else None,
                }
            )
            await session.closed.wait()
        finally:
            await self.close_session(session_id)

    async def propose(
        self, session_id: str, request: Propose, work: dict | None = None
    ) -> dict:
        """Retain one native proposal operation until its owning session can join cleanup."""
        task = self.start_operation(
            session_id, self._propose(session_id, request, work)
        )
        return await asyncio.shield(task)

    def apply_commit(self, session_id: str, request: Commit) -> None:
        """Apply one Target-confirmed delta atomically for manual commit or the next proposal."""
        session = self.draft_session(session_id)
        self.validate_tokens(request.token_ids)
        if (
            session.proposing
            or request.base_version != session.version
            or request.version != session.version + len(request.token_ids)
        ):
            raise HTTPException(409, "context changed or proposal is in flight")
        session.token_ids.extend(request.token_ids)
        session.version = request.version

    async def _propose(
        self, session_id: str, request: Propose, work: dict | None
    ) -> dict:
        """Generate against the confirmed prefix using native KV and local batching."""
        from vllm import SamplingParams
        from vllm.inputs import TokensPrompt
        from vllm.sampling_params import RequestOutputKind

        session = self.draft_session(session_id)
        if (
            self.max_candidate_tokens is not None
            and request.max_tokens > self.max_candidate_tokens
        ):
            raise HTTPException(
                422, "proposal exceeds the Draft method's candidate capacity"
            )
        if request.confirmed_delta is not None:
            if request.confirmed_delta.version != request.version:
                raise HTTPException(
                    409, "confirmed delta differs from proposal version"
                )
            self.apply_commit(session_id, request.confirmed_delta)
        if (
            request.version != session.version
            or request.version == session.proposed_version
            or session.proposing
        ):
            raise HTTPException(409, "context version changed or proposal is in flight")
        request_id = session_id
        artifact_id = uuid4().hex
        session.proposing = True
        logger.info(
            "DT proposal work=%s session=%s engine_request=%s",
            json.dumps(work),
            session_id,
            request_id,
        )
        if self.tensor_transports:
            session.artifacts.add(artifact_id)
        artifact = None
        context_task = None
        completed = False
        try:
            prompt = list(session.token_ids)
            extra = {"dt_version": request.version}
            if self.tensor_transports:
                extra["dt_artifact_id"] = artifact_id
            if work is not None:
                extra["dt_work"] = work
            if self.context_profile is not None:
                if request.context_source is None:
                    raise HTTPException(422, "neural Draft requires Target context")
                extra.update(
                    dt_context_session=session_id, dt_anchor_token=prompt.pop()
                )
            sampling = session.sampling.parameters()
            # Independent engines may share the same default global RNG seed.
            # Draw each proposal seed independently of Target's acceptance RNG.
            sampling["seed"] = randbits(63)
            params = SamplingParams(
                **sampling,
                extra_args=extra,
                max_tokens=request.max_tokens,
                ignore_eos=True,
                output_kind=RequestOutputKind.DELTA,
            )

            async def generate():
                """Admit only a live session after its required features have been appended."""
                if session.closed.is_set():
                    raise HTTPException(409, "Draft session closed during preparation")
                return await self.engine.propose(
                    request_id,
                    TokensPrompt(prompt_token_ids=prompt),
                    params,
                    session.priority,
                )

            if self.context_profile is not None:
                ready = asyncio.get_running_loop().create_future()
                context_task = self.defer_reclamation(
                    session_id,
                    self.receive_context(session_id, session, request, ready),
                )
                await self.wait_ready(session_id, context_task, ready)
            output = await generate()
            self.check_reclamations(session_id)
            tokens = list(output.outputs[0].token_ids)
            if session.closed.is_set():
                raise HTTPException(409, "session closed during proposal")
            if self.tensor_transports:
                await self.worker("dt_publish", artifact_id, session.tensor_transport)
                artifact = {"artifact_id": artifact_id}
            completed = True
            session.proposed_version = request.version
            return {
                "version": session.version,
                "token_ids": tokens,
                "artifact": artifact,
                "observation": output.dt_observation,
                "context_transfer": self.transfer_observations.pop(session_id, None),
            }
        finally:
            with CancelScope(shield=True):
                if not completed or session.closed.is_set():
                    await self.engine.abort(request_id)
                if context_task is not None and (
                    not completed or session.closed.is_set()
                ):
                    await asyncio.gather(context_task, return_exceptions=True)
                session.proposing = False
                if self.tensor_transports and (
                    artifact is None or session.closed.is_set()
                ):
                    await self.release_artifact(artifact_id)
                    session.artifacts.discard(artifact_id)

    async def receive_context(
        self, session_id: str, session: DraftSession, request: Propose, ready
    ):
        """Publish appended feature readiness; retain source ACK and destination reclamation."""
        source = request.context_source
        binding = ContextSource(endpoint=source.endpoint, session_id=source.session_id)
        if session.context_source is not None and binding != session.context_source:
            raise HTTPException(409, "Draft context source changed within its session")
        session.context_source = binding
        start_token = session.context_tokens
        if source.context is not None:
            context = source.context.model_dump()
            export_seconds = source.context.export_seconds
        else:
            # Manual diagnostics may still request an interval from an unbound Target.
            exporting = time.monotonic()
            context = await self.communication.export_context(
                source.endpoint,
                source.session_id,
                request.version,
                session.tensor_transport,
                start_token,
            )
            export_seconds = time.monotonic() - exporting
        if context is None:
            raise HTTPException(409, CONTEXT_REVOKED)
        artifact_id = context["artifact_id"]
        logger.info(
            "DT context work=%s artifact=%s source=%s",
            json.dumps(request.work),
            artifact_id,
            source.endpoint,
        )
        # Keep failed receive/release ownership visible to session cleanup too.
        session.artifacts.add(artifact_id)

        async def validate(payload):
            if session.closed.is_set():
                raise HTTPException(409, "Draft session closed during preparation")
            if payload.transport != session.tensor_transport:
                raise HTTPException(
                    422, "context transport differs from session binding"
                )
            if (
                context["profile"] != self.context_profile
                or context["token_ids"] != session.token_ids
                or context["start_token"] != start_token
                or context["end_token"] != len(session.token_ids) - 1
            ):
                raise HTTPException(
                    422, "Target context profile or confirmed tokens differ"
                )
            if (
                len(payload.shape) != 3
                or payload.shape[0] != context["end_token"] - start_token
            ):
                raise HTTPException(
                    422, "Target context does not cover the computed prefix"
                )

        started = time.monotonic()
        fused = session.proposed_version is not None
        retained = False
        stage_rpc = RpcTiming()

        async def receive(payload, rpc):
            """Retain uncertain native consumption until session abort and cleanup fence it."""
            nonlocal retained
            retained = True
            result = await self.engine.receive_draft_context(
                session_id,
                request.version,
                start_token,
                context["end_token"],
                artifact_id,
                payload.descriptor(),
                rpc,
                stage_rpc,
            )
            retained = result["append_error"] is not None
            return result

        async def append(result):
            """Make features compute-ready before waiting for independent source reclamation."""
            if fused and result["append_error"] is not None:
                raise RuntimeError(result["append_error"])
            if session.closed.is_set():
                raise HTTPException(409, "Draft session closed during preparation")
            if fused:
                if not result["appended"]:
                    raise HTTPException(409, "Draft context changed during preparation")
            else:
                await self.worker(
                    "dt_append_draft_context",
                    session_id,
                    artifact_id,
                    start_token,
                    context["end_token"],
                    timing=stage_rpc,
                )
            session.context_tokens = context["end_token"]
            result["breakdown"].update(
                export_seconds=export_seconds,
                stage_rpc=stage_rpc.to_wire(),
                total_seconds=export_seconds + time.monotonic() - started,
            )
            self.transfer_observations[session_id] = {
                "bytes": result["bytes"],
                "transport": result["transport"],
                "read_seconds": result["read_seconds"],
                "prepare_seconds": time.monotonic() - started,
                "breakdown": result["breakdown"],
            }
            ready.set_result(None)

        try:
            result = await self.communication.read_artifact(
                artifact_id,
                source.endpoint,
                validate,
                on_ready=append,
                receive=receive if fused else None,
                payload=context.get("payload"),
            )
            if result is None:
                raise HTTPException(409, CONTEXT_REVOKED)
        finally:
            releasing = time.monotonic()
            if not retained:
                await self.release_artifact(artifact_id)
                session.artifacts.discard(artifact_id)
        result["breakdown"].update(
            release_seconds=time.monotonic() - releasing,
        )
        logger.info(
            "DT feature prepared session=%s version=%s artifact=%s",
            session_id,
            request.version,
            artifact_id,
        )

    def prepare_context(
        self, session_id: str, ticket, transport: str, start_token: int
    ):
        """Start publication on confirmation; session close retains the task and unclaimed source."""
        artifact_id = uuid4().hex
        task = self.start_operation(
            session_id, self.export_context(ticket, artifact_id, transport, start_token)
        )
        self.target_exports.setdefault(session_id, {})[artifact_id] = task
        return task

    async def claim_artifact(self, artifact_id: str) -> dict:
        """Hand off a prepared context once, or delegate an ordinary claim to its Worker."""
        for session_id, exports in self.target_exports.items():
            preparation = exports.get(artifact_id)
            if preparation is not None:
                if session_id not in self.targets:
                    raise HTTPException(
                        409, "Target generation closed before context claim"
                    )
                # Reserve before yielding so close and duplicate claims cannot revoke or reuse it.
                exports[artifact_id] = None
                context = await asyncio.shield(preparation)
                return context["payload"]
        timing = RpcTiming()
        payload = await self.worker("dt_claim", artifact_id, timing=timing)
        if payload is None:
            raise HTTPException(409, "artifact is unavailable or already claimed")
        return {**payload, "claim_rpc": timing.to_wire()}

    async def export_context(self, ticket, artifact_id, transport, start_token):
        """Publish a scheduler-selected snapshot while retaining session cancellation ownership."""
        started = time.monotonic()
        context = await self.engine.export_context(
            ticket, artifact_id, start_token, transport
        )
        if context is None:
            raise HTTPException(409, "Target generation changed")
        # The session inventory retains this reserved claim until remote handoff
        # or close proves that the descriptor never left this process.
        logger.info(
            "DT context publication artifact=%s engine_request=%s frontier=%d interval=[%d,%d)",
            artifact_id,
            ticket.request_id,
            ticket.generation,
            start_token,
            context["end_token"],
        )
        return {
            **context,
            "profile": self.context_profile,
            "export_seconds": time.monotonic() - started,
        }

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
        coordinator = None
        if request.draft_endpoints:
            from .coordinator import TargetCoordinator

            coordinator = TargetCoordinator(self, session_id, request)
            self.coordinators[session_id] = coordinator
        try:
            if coordinator is not None and not await coordinator.peer.open(
                request, coordinator.frontier
            ):
                yield _event({"event": "admission_closed"})
                return
            yield _event(
                {
                    "event": "opened",
                    "session_id": session_id,
                    "rdma": self.communication.identity.model_dump()
                    if self.communication.identity
                    else None,
                }
            )
            if coordinator is not None:
                await coordinator.prepare_binding()
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
                task = self.start_operation(
                    session_id, self.receive_checkpoint(session_id, resume)
                )
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
            work = coordinator.identity("prefill") if coordinator else request.work
            if work is not None:
                params.extra_args = {
                    **(params.extra_args or {}),
                    "dt_work": work,
                }
            async with aclosing(
                self.engine.generate(
                    TokensPrompt(prompt_token_ids=request.token_ids),
                    params,
                    session_id,
                    priority=request.priority,
                )
            ) as outputs:
                async for output in outputs:
                    if session_id not in self.targets:
                        return
                    self.check_reclamations(session_id)
                    artifact_id = self.target_artifacts.pop(session_id, None)
                    if artifact_id is not None:
                        self.defer_reclamation(
                            session_id, self.release_artifact(artifact_id)
                        )
                    ticket = output.external_draft_request
                    self.targets[session_id] = ticket
                    completion = output.outputs[0]
                    transfer = self.transfer_observations.pop(session_id, None)
                    event = {
                        "event": "committed",
                        "token_ids": list(completion.token_ids),
                        "text": completion.text,
                        "version": ticket.generation if ticket is not None else None,
                        "finished": output.finished,
                        "finish_reason": completion.finish_reason,
                        "stop_reason": completion.stop_reason,
                        "cached_token_count": output.num_cached_tokens or 0,
                        "observation": output.dt_observation,
                        "transfer": transfer,
                        "round": None,
                    }
                    if coordinator is not None:
                        coordinator.confirmed(event)
                    if session_id not in self.targets:
                        return
                    yield _event(event)
                    if coordinator is not None and ticket is not None:
                        await coordinator.advance(ticket)
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
            await self.worker("dt_publish", artifact_id, "rdma")
            return checkpoint
        finally:
            if session_id not in self.targets:
                await self.release_artifact(artifact_id)

    async def receive_checkpoint(self, session_id: str, resume: ResumeTarget):
        """Own checkpoint claim/read/ACK and retain the receiver through its first forward."""
        checkpoint = resume.checkpoint
        artifact_id = checkpoint.artifact_id
        self.target_artifacts[session_id] = artifact_id
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
            result = await self.communication.read_artifact(
                artifact_id, resume.source_endpoint, validate
            )
            if result is None:
                raise HTTPException(409, "source checkpoint was revoked")
            if session_id not in self.targets:
                return
            self.target_artifacts[session_id] = artifact_id
            prepared = time.monotonic() - started
            self.transfer_observations[session_id] = {
                "bytes": result["bytes"],
                "transport": result["transport"],
                "read_seconds": result["read_seconds"],
                "prepare_seconds": prepared,
                "breakdown": {**result["breakdown"], "total_seconds": prepared},
            }
            retained = True
        finally:
            if not retained:
                await self.release_artifact(artifact_id)
                self.target_artifacts.pop(session_id, None)

    async def verify(
        self, session_id: str, request: Verify, work: dict | None = None
    ) -> bool:
        """Prepare one member independently, then let native ticket admission decide readiness."""
        self.require_role("target")
        self.check_reclamations(session_id)
        self.validate_tokens(request.token_ids)
        if len(request.token_ids) > self.max_candidate_tokens:
            raise HTTPException(
                422, "proposal exceeds the Target engine's candidate capacity"
            )
        ticket = self.targets.get(session_id)
        if (
            ticket is None
            or ticket.generation != request.version
            or session_id in self.verifying
        ):
            return False
        needs_tensor = bool(self.tensor_transports and request.token_ids)
        if needs_tensor != (request.artifact is not None):
            raise HTTPException(
                422, "proposal tensor does not match the selected sampling mode"
            )
        if needs_tensor and request.source_endpoint is None:
            raise HTTPException(422, "proposal tensor requires its source endpoint")
        self.verifying.add(session_id)
        logger.info(
            "DT verification work=%s session=%s version=%s artifact=%s",
            json.dumps(work),
            session_id,
            request.version,
            request.artifact.artifact_id if request.artifact is not None else None,
        )

        async def prepare_and_admit():
            from .vllm.engine import ExternalDraftSubmission

            try:
                if needs_tensor:
                    return await self.receive_and_admit(
                        session_id, ticket, request, work, ready
                    )
                if self.targets.get(session_id) != ticket:
                    return False
                # Consume role-side ownership before the utility call. A lost
                # admission response cannot make the same ticket submit again.
                self.targets[session_id] = None
                (accepted,) = await self.engine.submit_external_draft_batch(
                    [ExternalDraftSubmission(ticket, request.token_ids, work)]
                )
                if not accepted:
                    artifact = self.target_artifacts.get(session_id)
                    if artifact is not None:
                        await self.release_artifact(artifact)
                        self.target_artifacts.pop(session_id, None)
                return accepted
            finally:
                # A successful ready notification releases this round's guard.
                # Its later ACK must not clear a newer round's preparation.
                if not needs_tensor or not ready.done():
                    self.verifying.discard(session_id)

        if needs_tensor:
            ready = asyncio.get_running_loop().create_future()
            task = self.defer_reclamation(session_id, prepare_and_admit())
            return await self.wait_ready(session_id, task, ready)
        task = self.start_operation(session_id, prepare_and_admit())
        return await asyncio.shield(task)

    def _operation_finished(self, task: asyncio.Task) -> None:
        """Observe failures even when the initiating HTTP request disconnected."""
        self.operations.discard(task)
        if not task.cancelled():
            error = task.exception()
            if isinstance(error, HTTPException) and error.status_code == 409:
                logger.info("DT operation rejected: %s", error.detail)
            elif error is not None:
                logger.error(
                    "DT operation failed",
                    exc_info=(type(error), error, error.__traceback__),
                )

    async def drain_transfers(self) -> None:
        """Join network/session work and drain Worker buffers before lifespan closes the HTTP pool."""
        results = await asyncio.gather(
            self.communication.drain(), *tuple(self.operations), return_exceptions=True
        )
        if self.tensor_transports:
            while True:
                result = await self.worker("dt_close")
                if result["ready"]:
                    break
                status = await self.worker("dt_status")
                if (
                    status["retained_artifacts"] > 0
                    and status["retained_artifacts"]
                    == status["unacknowledged_publications"]
                ):
                    # Without a remote ACK, process termination remains the
                    # safe release boundary; never unregister these buffers.
                    logger.warning(
                        "Terminating Worker with %d unacknowledged DT publications",
                        status["unacknowledged_publications"],
                    )
                    break
                await asyncio.sleep(0.001)
        for result in results:
            if isinstance(result, BaseException):
                raise result

    async def receive_and_admit(
        self, session_id, ticket, request: Verify, work, ready
    ) -> bool:
        """Submit a completed read immediately, overlapping verification with the retained ACK."""
        from .vllm.engine import ExternalDraftSubmission

        started = time.monotonic()
        artifact_id = request.artifact.artifact_id
        self.target_artifacts[session_id] = artifact_id
        retained = False
        accepted = False
        stage_rpc = RpcTiming()

        async def validate(payload):
            if self.targets.get(session_id) != ticket:
                raise HTTPException(409, "Target generation changed")
            coordinator = self.coordinators.get(session_id)
            if (
                coordinator is not None
                and payload.transport != coordinator.peer.tensor_transport
            ):
                raise HTTPException(
                    422, "proposal transport differs from session binding"
                )
            expected = [
                len(request.token_ids),
                self.engine.model_config.get_vocab_size(),
            ]
            if payload.dtype != "float32" or payload.shape != expected:
                raise HTTPException(
                    422, "Draft distribution does not match candidates and vocabulary"
                )

        async def receive(payload, rpc):
            """Keep the destination through the native read/admission call, even if its reply is lost."""
            nonlocal retained
            self.targets[session_id] = None
            retained = True
            return await self.engine.receive_external_draft(
                ExternalDraftSubmission(
                    ticket, request.token_ids, work, artifact_id, stage_rpc
                ),
                payload.descriptor(),
                rpc,
            )

        async def admit(result):
            """Publish a completed native admission while the same operation joins source ACK."""
            nonlocal retained, accepted
            accepted = result["accepted"]
            retained = accepted
            prepared = time.monotonic() - started
            result["breakdown"].update(
                stage_rpc=stage_rpc.to_wire(),
                total_seconds=prepared,
            )
            if session_id in self.targets:
                self.transfer_observations[session_id] = {
                    "bytes": result["bytes"],
                    "transport": result["transport"],
                    "read_seconds": result["read_seconds"],
                    "prepare_seconds": prepared,
                    "breakdown": result["breakdown"],
                }
            self.verifying.discard(session_id)
            ready.set_result(accepted)

        try:
            result = await self.communication.read_artifact(
                artifact_id,
                request.source_endpoint,
                validate,
                on_ready=admit,
                receive=receive,
            )
            if result is None:
                raise HTTPException(
                    409, "proposal artifact was revoked or already claimed"
                )
            return accepted
        finally:
            if not retained:
                await self.release_artifact(artifact_id)
                self.target_artifacts.pop(session_id, None)


def create_app(
    engine_args: AsyncEngineArgs,
    role: Literal["draft", "target"],
    default_draft_tokens: int | None,
    rdma_host: str | None = None,
    rdma_nic: str = "",
    zmq_endpoint: str | None = None,
    gpu_ipc_host: str | None = None,
) -> FastAPI:
    """Build an internal role API; its lifespan exclusively owns the vLLM engine."""
    from vllm.usage.usage_lib import UsageContext

    from .vllm.engine import ExternalAsyncLLM as AsyncLLM

    engine_args.additional_config = {
        **(engine_args.additional_config or {}),
        "foretoken_dt_role": role,
    }
    config = engine_args.create_engine_config(UsageContext.ENGINE_CONTEXT)
    max_candidate_tokens = (
        config.speculative_config.num_speculative_tokens
        if config.speculative_config is not None
        else None
    )
    if role == "target" and (
        default_draft_tokens is None
        or default_draft_tokens <= 0
        or max_candidate_tokens is None
        or default_draft_tokens > max_candidate_tokens
    ):
        raise ValueError(
            "Target default candidates must be positive and fit engine capacity"
        )

    # The managed launcher preserves provider identity when loading local snapshots.
    metadata = json.loads(os.environ.get("FORETOKEN_DT_MODEL_METADATA", "{}"))
    tensor_transports = tuple(
        name
        for name, endpoint in (
            ("rdma", rdma_host),
            ("gpu_ipc", gpu_ipc_host),
            ("zmq", zmq_endpoint),
        )
        if endpoint is not None
    )

    @asynccontextmanager
    async def lifespan(app):
        engine = AsyncLLM.from_vllm_config(
            config,
            enable_log_requests=engine_args.enable_log_requests,
            disable_log_stats=engine_args.disable_log_stats,
        )
        service = RoleService(
            engine,
            role,
            default_draft_tokens,
            max_candidate_tokens,
            tensor_transports,
            metadata,
        )
        app.state.service = service
        connector_initialized = False
        try:
            if service.tensor_transports:
                transport = await service.worker(
                    "dt_initialize",
                    role,
                    rdma_host,
                    rdma_nic,
                    zmq_endpoint,
                    gpu_ipc_host,
                )
                service.tensor_transports = tuple(transport["tensor_transports"])
                service.gpu_ipc = transport["gpu_ipc"]
                if transport["rdma"] is not None:
                    service.communication.identity = RdmaIdentity.model_validate(
                        transport["rdma"]
                    )
                connector_initialized = True
            yield
        finally:
            service.accepting = False
            try:
                sessions = (
                    service.drafts.keys()
                    | service.targets.keys()
                    | service.closing.keys()
                )
                results = await asyncio.gather(
                    *(service.close_session(session_id) for session_id in sessions),
                    return_exceptions=True,
                )
                try:
                    if connector_initialized or not service.tensor_transports:
                        await service.drain_transfers()
                finally:
                    for result in results:
                        if isinstance(result, BaseException):
                            raise result
            finally:
                try:
                    await service.communication.close()
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
            "active_sessions": len(
                service.drafts.keys() | service.targets.keys() | service.closing.keys()
            ),
            "candidate_format": "token_ids_log_probs"
            if service.tensor_transports
            else "greedy_token_ids",
            "tensor_transports": service.tensor_transports,
            "gpu_ipc": service.gpu_ipc,
            "rdma": service.communication.identity.model_dump()
            if service.communication.identity
            else None,
            "rdma_failed_peers": [
                peer.instance_id
                for peer, connection in service.communication.peers.items()
                if connection.close_task is not None
            ],
            "default_draft_tokens": service.default_draft_tokens,
            "max_candidate_tokens": service.max_candidate_tokens,
            "kv_migration": service.migration_enabled,
            "context_profile": service.context_profile,
            "protocol_version": ROLE_PROTOCOL_VERSION,
        }
        if observations:
            transport = (
                await service.worker("dt_status") if service.tensor_transports else None
            )
            result["retained_artifacts"] = (
                transport["retained_artifacts"] if transport else 0
            )
            result["transport"] = transport
            result["rdma_connections"] = {
                "peers": len(service.communication.peers),
                "verifications": service.communication.verifications,
                "pending_operations": sum(
                    len(peer.operations)
                    for peer in service.communication.peers.values()
                ),
            }
            result["workload"] = await service.engine.workload()
        return result

    @app.get("/healthz")
    @app.get("/readyz")
    async def health():
        """Keep the Service endpoint reachable for existing rounds during drain."""
        await status(observations=False)
        return {"healthy": True}

    @app.post("/profiling")
    async def profiling(request: ProfileOperation):
        """Bridge supervised capture to native utilities without creating another capture owner."""
        service = app.state.service
        if request.directory is not None:
            await service.worker("foretoken_mctracer", request.start, request.directory)
        elif request.start:
            await service.engine.start_profile(profile_prefix=request.profile_prefix)
        else:
            await service.engine.stop_profile()
        return {"completed": True}

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
            # Each role currently owns one EngineCore rank. Publish its native
            # gauges explicitly for the common rank-aware Router scorers.
            "data_parallel_ranks": [
                {
                    "data_parallel_rank": 0,
                    "scheduler_running_requests": workload["running_requests"],
                    "scheduler_waiting_requests": workload["waiting_requests"],
                    "kv_cache_usage": workload["kv_cache_usage"],
                }
            ],
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
        if request.tensor_transport is None:
            request.tensor_transport = (
                service.tensor_transports[0] if service.tensor_transports else None
            )
        elif request.tensor_transport not in service.tensor_transports:
            raise HTTPException(422, "selected tensor transport is not initialized")
        return StreamingResponse(
            service.open_draft(request), media_type="application/x-ndjson"
        )

    @app.get("/sessions/{session_id}/state")
    async def session_state(session_id: str):
        """Report the existing close owner; closed proves native and buffer cleanup finished."""
        service = app.state.service
        closing = service.closing.get(session_id)
        if closing is not None:
            if closing.done() and (
                closing.cancelled() or closing.exception() is not None
            ):
                return {"state": "failed"}
            return {"state": "closing"}
        return {
            "state": "open"
            if session_id in service.drafts or session_id in service.targets
            else "closed"
        }

    @app.post("/transport/rdma/probe")
    async def transport_probe(request: TransportPeer):
        """Publish for a transport incarnation; the communication owner survives caller cancellation."""
        service = app.state.service
        connection = service.communication.accept(
            request.identity, request.peer, request.peer_endpoint
        )
        artifact_id = await asyncio.shield(connection.start(connection.publish()))
        return {"artifact_id": artifact_id}

    @app.post("/transport/rdma/probe/read")
    async def read_transport_probe(request: ReadTransportProbe):
        """Read, ACK and verify a connection-owned probe independently of model sessions."""
        service = app.state.service
        connection = service.communication.accept(
            request.identity, request.peer, request.peer_endpoint
        )
        return await asyncio.shield(
            connection.start(connection.read(request.artifact_id))
        )

    @app.get("/sessions/{session_id}/kv")
    async def request_kv(session_id: str):
        """Read current session residency for a placement decision; this is not a KV lease."""
        service = app.state.service
        if service.role == "target":
            if session_id not in service.targets:
                raise HTTPException(404, "target session is closed or unknown")
        else:
            service.draft_session(session_id)
        return {
            "collected_at_unix_ms": time.time_ns() // 1_000_000,
            "kv": await service.engine.request_kv(session_id),
        }

    @app.post("/sessions/{session_id}/propose")
    async def propose(session_id: str, request: Propose):
        """Return one candidate chain; the owning session stream controls abort."""
        return await app.state.service.propose(session_id, request, work=request.work)

    @app.post("/sessions/{session_id}/commit")
    async def commit(session_id: str, request: Commit):
        """Apply the exact Target delta before accepting the next Draft round."""
        app.state.service.apply_commit(session_id, request)
        return {"version": request.version}

    def validate_generation(request: Generate):
        """Validate both fresh and migrated Target admission at their shared boundary."""
        service = app.state.service
        service.require_role("target")
        service.validate_tokens(request.token_ids)
        service.validate_tokens(request.stop_token_ids)
        service.validate_sampling(request.sampling)
        if len(request.draft_endpoints) > 1:
            raise HTTPException(422, "this protocol currently supports one bound Draft")
        if request.draft_endpoints and (
            not request.request_id or not request.target_endpoint
        ):
            raise HTTPException(
                422, "a routed binding requires request_id and target_endpoint"
            )
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
        if session_id in service.coordinators:
            raise HTTPException(
                409, "checkpoint requires an unbound diagnostic session"
            )
        if not service.migration_enabled:
            raise HTTPException(422, "Target KV migration is disabled")
        ticket = service.targets.get(session_id)
        if ticket is None or ticket.generation != request.version:
            raise HTTPException(409, "Target generation changed")
        artifact_id = uuid4().hex
        service.target_exports.setdefault(session_id, {})[artifact_id] = None
        task = service.start_operation(
            session_id, service.export_checkpoint(session_id, ticket, artifact_id)
        )
        return await asyncio.shield(task)

    @app.post("/sessions/{session_id}/context")
    async def context(session_id: str, request: ExportContext):
        """Export the confirmed feature prefix for a neural Draft proposal."""
        service = app.state.service
        service.require_role("target")
        if service.context_profile is None:
            raise HTTPException(422, "Target feature export is disabled")
        if request.tensor_transport not in service.tensor_transports:
            raise HTTPException(422, "context tensor transport is not initialized")
        coordinator = service.coordinators.get(session_id)
        if (
            coordinator is not None
            and request.tensor_transport != coordinator.peer.tensor_transport
        ):
            raise HTTPException(422, "context transport differs from session binding")
        ticket = service.targets.get(session_id)
        if ticket is None or ticket.generation != request.version:
            raise HTTPException(409, "Target generation changed")
        artifact_id = uuid4().hex
        service.target_exports.setdefault(session_id, {})[artifact_id] = None
        task = service.start_operation(
            session_id,
            service.export_context(
                ticket,
                artifact_id,
                request.tensor_transport,
                request.start_token,
            ),
        )
        return await asyncio.shield(task)

    @app.post("/resume")
    async def resume(request: ResumeTarget):
        """Receive KV before admitting generation from the same confirmed prefix."""
        service = app.state.service
        validate_generation(request.generation)
        if (
            not service.migration_enabled
            or request.generation.draft_endpoints
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
        if session_id in service.coordinators:
            raise HTTPException(409, "the bound Target owns this request's rounds")
        return {"accepted": await service.verify(session_id, request)}

    @app.post("/artifacts/{artifact_id}/claim")
    async def claim_artifact(artifact_id: str):
        """Grant a peer one descriptor unless cancellation already revoked it."""
        return await app.state.service.claim_artifact(artifact_id)

    @app.post("/artifacts/{artifact_id}/release")
    async def release_artifact(artifact_id: str, request: ReleaseArtifact):
        """Release a claimed publication after its reader guarantees no further read."""
        service = app.state.service
        timing = RpcTiming()
        await service.release_artifact(
            artifact_id, request.publication_id, timing=timing
        )
        service.communication.released(artifact_id)
        for session in service.drafts.values():
            session.artifacts.discard(artifact_id)
        for exports in service.target_exports.values():
            exports.pop(artifact_id, None)
        return {"released": True, "rpc": timing.to_wire()}

    @app.delete("/sessions/{session_id}")
    async def close(session_id: str):
        """Idempotently close a session and abort its active engine request."""
        await app.state.service.close_session(session_id)
        return {"closed": True}

    @app.delete("/artifacts/{artifact_id}")
    async def revoke_artifact(artifact_id: str):
        """Revoke unused work without releasing a source whose consumer still owes an ACK."""
        service = app.state.service
        await service.release_artifact(artifact_id)
        for session in service.drafts.values():
            session.artifacts.discard(artifact_id)
        return {"revoked": True}

    return app
