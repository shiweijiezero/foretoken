# SPDX-License-Identifier: Apache-2.0
# SPDX-FileCopyrightText: Copyright contributors to the Foretoken project

"""Role-owned HTTP sessions, peer readiness and tensor transfer coordination."""

from __future__ import annotations

import asyncio
import json
import logging
from typing import TYPE_CHECKING

import httpx
from anyio import CancelScope
from fastapi import HTTPException
from pydantic import BaseModel, ConfigDict

if TYPE_CHECKING:
    from .service import RoleService

logger = logging.getLogger(__name__)


class RdmaIdentity(BaseModel):
    """Identify one initialized Mooncake engine on its actual Worker GPU and NICs."""

    model_config = ConfigDict(extra="forbid", frozen=True)
    instance_id: str
    segment: str


class RdmaConnection:
    """Own a peer's shared proof, probe artifacts and operations until drain."""

    def __init__(self, owner: RoleCommunication, peer: RdmaIdentity, endpoint: str):
        """Retain metadata and tasks for one local/remote transport incarnation pair."""
        self.owner = owner
        self.peer = peer
        self.endpoint = endpoint
        self.artifacts: set[str] = set()
        self.operations: set[asyncio.Task] = set()
        self.proof: asyncio.Task | None = None
        self.close_task: asyncio.Task | None = None

    def start(self, operation) -> asyncio.Task:
        """Retain communication work independently of an HTTP caller's cancellation."""
        task = self.owner.start(operation)
        self.operations.add(task)

        def completed(task):
            self.operations.discard(task)
            if task.cancelled() or task.exception() is not None:
                self.retire()

        task.add_done_callback(completed)
        return task

    async def prepare(self, local_endpoint: str) -> None:
        """Share one bidirectional proof; a cancelled waiter does not cancel its owner."""
        if self.close_task is not None:
            raise HTTPException(
                503, "RDMA connection is invalid; peer re-establishment required"
            )
        if self.proof is None:
            self.proof = self.start(self._verify(local_endpoint))
        await asyncio.shield(self.proof)
        if self.close_task is not None:
            raise HTTPException(
                503, "RDMA connection was invalidated during verification"
            )

    async def _verify(self, local_endpoint: str) -> None:
        """Establish both GPU directions with ordinary publication, read and ACK owners."""
        assert self.owner.identity is not None
        body = {
            "identity": self.peer.model_dump(),
            "peer": self.owner.identity.model_dump(),
            "peer_endpoint": local_endpoint,
        }
        self.owner.verifications += 1
        logger.info(
            "DT RDMA peer verification start local=%s peer=%s count=%s",
            self.owner.identity.instance_id,
            self.peer.instance_id,
            self.owner.verifications,
        )
        response = await self.owner.client.post(
            f"{self.endpoint}/transport/rdma/probe", json=body, timeout=None
        )
        response.raise_for_status()
        incoming = await self.read(response.json()["artifact_id"])
        artifact_id = await self.publish()
        try:
            response = await self.owner.client.post(
                f"{self.endpoint}/transport/rdma/probe/read",
                json={**body, "artifact_id": artifact_id},
                timeout=None,
            )
            response.raise_for_status()
            logger.info(
                "DT RDMA peer verified local=%s peer=%s incoming=%s outgoing=%s",
                self.owner.identity.instance_id,
                self.peer.instance_id,
                incoming,
                response.json(),
            )
        finally:
            await self.revoke(artifact_id)

    async def publish(self) -> str:
        """Retain a GPU probe until the existing artifact ACK or safe revocation releases it."""
        from uuid import uuid4

        artifact_id = uuid4().hex
        self.artifacts.add(artifact_id)
        service = self.owner.service
        try:
            await service.worker("dt_create_transport_probe", artifact_id)
            while not (await service.worker("dt_publish", artifact_id, "rdma"))[
                "ready"
            ]:
                await asyncio.sleep(0.001)
        except BaseException:
            await self.revoke(artifact_id)
            raise
        return artifact_id

    async def revoke(self, artifact_id: str) -> None:
        """Join safe source revocation; a claimed probe remains owned until ACK or process exit."""
        while True:
            released = await self.owner.service.worker("dt_release", artifact_id)
            if released is None:
                return
            if released:
                self.artifacts.discard(artifact_id)
                return
            await asyncio.sleep(0.001)

    async def read(self, artifact_id: str) -> dict:
        """Validate this peer's GPU data and keep failed or uncertain storage quarantined."""
        service = self.owner.service
        self.artifacts.add(artifact_id)

        async def validate(payload):
            if (
                payload.transport != "rdma"
                or payload.segment != self.peer.segment
                or payload.dtype != "float32"
                or payload.shape != [1, service.engine.model_config.get_vocab_size()]
            ):
                raise HTTPException(422, "invalid RDMA peer probe")

        try:
            result = await self.owner.read_artifact(
                artifact_id, self.endpoint, validate
            )
            if result is None:
                raise HTTPException(409, "RDMA peer probe was revoked")
            await service.worker("dt_check_transport_probe", artifact_id)
            return result
        finally:
            await service.release_artifact(artifact_id)
            self.artifacts.discard(artifact_id)

    def retire(self) -> asyncio.Task:
        """Invalidate once and retain cleanup independently of peer replacement or request waiters."""
        if self.close_task is None:
            self.close_task = self.owner.start(self._close())
        return self.close_task

    async def close(self) -> None:
        """Join the same retirement owner used by failure and peer replacement."""
        await asyncio.shield(self.retire())

    async def _close(self) -> None:
        """Join started operations, revoke unclaimed probes and retain claimed storage for ACK."""
        while self.operations:
            await asyncio.gather(*tuple(self.operations), return_exceptions=True)
        for artifact_id in tuple(self.artifacts):
            await self.revoke(artifact_id)


class RoleCommunication:
    """Own the role HTTP pool and communication tasks; workers retain physical tensor storage."""

    def __init__(self, service: RoleService):
        """Attach shared communication work to the role's existing engine drain owner."""
        self.service = service
        # Request admission owns capacity. Long-lived session streams must not
        # consume a second connection limit and starve control messages or ACKs.
        self.client = httpx.AsyncClient(limits=httpx.Limits(max_connections=None))
        self.sessions: set[DraftPeerSession] = set()
        self.operations: set[asyncio.Task] = set()
        self.identity: RdmaIdentity | None = None
        self.peers: dict[RdmaIdentity, RdmaConnection] = {}
        self.verifications = 0
        self.closing = False

    def select_transport(
        self, peer_transports, peer_gpu_ipc: dict | None
    ) -> str | None:
        """Select a common initialized path at binding; active transfers never switch paths."""
        for transport in ("gpu_ipc", "rdma", "zmq"):
            if (
                transport in self.service.tensor_transports
                and transport in peer_transports
            ):
                if transport == "gpu_ipc" and not (
                    self.service.gpu_ipc is not None
                    and peer_gpu_ipc is not None
                    and self.service.gpu_ipc["host_id"] == peer_gpu_ipc["host_id"]
                    and self.service.gpu_ipc["device_id"]
                    in peer_gpu_ipc["peer_devices"]
                    and peer_gpu_ipc["device_id"]
                    in self.service.gpu_ipc["peer_devices"]
                ):
                    continue
                return transport
        if self.service.tensor_transports or peer_transports:
            raise ValueError("selected roles have no common tensor transport")
        return None

    def _get(self, peer: RdmaIdentity, endpoint: str) -> RdmaConnection:
        """Resolve the current peer incarnation; retire old identities without cancelling DMA."""
        if self.closing or self.identity is None:
            raise HTTPException(503, "RDMA connections are unavailable")
        endpoint = endpoint.rstrip("/")
        for identity, connection in tuple(self.peers.items()):
            if connection.endpoint == endpoint and identity != peer:
                connection.retire()
                if not connection.operations and not connection.artifacts:
                    del self.peers[identity]
        connection = self.peers.get(peer)
        if connection is None:
            connection = RdmaConnection(self, peer, endpoint)
            self.peers[peer] = connection
        elif connection.endpoint != endpoint:
            raise HTTPException(409, "RDMA peer control endpoint changed")
        if connection.close_task is not None:
            raise HTTPException(503, "RDMA peer connection is invalid")
        return connection

    def bind(self, status: dict, endpoint: str) -> RdmaConnection:
        """Borrow live peer readiness, including failures observed by its receiving Worker."""
        connection = self._get(RdmaIdentity.model_validate(status["rdma"]), endpoint)
        if self.identity.instance_id in status["rdma_failed_peers"]:
            connection.retire()
            raise HTTPException(503, "peer invalidated this RDMA connection")
        return connection

    def accept(
        self, identity: RdmaIdentity, peer: RdmaIdentity, endpoint: str
    ) -> RdmaConnection:
        """Reject a stale HTTP address before publishing or reading any GPU storage."""
        if identity != self.identity:
            raise HTTPException(409, "RDMA transport instance changed")
        return self._get(peer, endpoint)

    def released(self, artifact_id: str) -> None:
        """Forget a source artifact only after its Worker accepted the ordinary release ACK."""
        for connection in self.peers.values():
            connection.artifacts.discard(artifact_id)

    def failed(self, segment: str) -> None:
        """Invalidate proofs after a failed data transfer; never replay the in-flight read."""
        for connection in self.peers.values():
            if connection.peer.segment == segment:
                connection.retire()

    def start(self, operation) -> asyncio.Task:
        """Retain one network/transfer operation independently of a cancelled waiter."""
        task = asyncio.create_task(operation)
        self.operations.add(task)

        def completed(task):
            self.operations.discard(task)
            if not task.cancelled() and (error := task.exception()) is not None:
                if isinstance(error, HTTPException) and error.status_code == 409:
                    logger.info("DT communication rejected: %s", error.detail)
                else:
                    logger.error(
                        "DT communication failed",
                        exc_info=(type(error), error, error.__traceback__),
                    )

        task.add_done_callback(completed)
        return task

    async def export_context(
        self, endpoint: str, session_id: str, version: int, transport: str
    ) -> dict | None:
        """Obtain a confirmed feature handle; the calling engine stage validates its model context."""
        response = await self.client.post(
            f"{endpoint.rstrip('/')}/sessions/{session_id}/context",
            json={"version": version, "tensor_transport": transport},
            timeout=None,
        )
        if response.status_code == 409:
            return None
        response.raise_for_status()
        return response.json()

    async def read_artifact(
        self, artifact_id: str, source_endpoint: str, validate
    ) -> dict | None:
        """Join claim/read/ACK; the caller owns destination staging after successful completion."""
        return await asyncio.shield(
            self.start(self._read_artifact(artifact_id, source_endpoint, validate))
        )

    async def _read_artifact(self, artifact_id, source_endpoint, validate):
        """Own one claim/read/ACK; failed reads retain storage until DMA is known complete."""
        from .service import TensorPayload

        service = self.service
        read_started = False
        read_finished = False
        try:
            source = f"{source_endpoint.rstrip('/')}/artifacts/{artifact_id}"
            response = await self.client.post(f"{source}/claim")
            if response.status_code == 409:
                return None
            response.raise_for_status()
            payload = TensorPayload.model_validate(response.json())
            try:
                if payload.transport not in service.tensor_transports:
                    raise HTTPException(
                        422,
                        "source tensor transport is not initialized by receiver",
                    )
                await validate(payload)
                while True:
                    read_started = True
                    result = await service.worker(
                        "dt_receive", artifact_id, payload.descriptor()
                    )
                    if result["ready"]:
                        read_finished = True
                        logger.info(
                            "DT transfer completed artifact=%s source=%s observation=%s",
                            artifact_id,
                            source_endpoint,
                            json.dumps(result),
                        )
                        return result
                    await asyncio.sleep(0.001)
            finally:
                if not read_started or read_finished:
                    response = await self.client.post(
                        f"{source}/release",
                        json={"publication_id": payload.publication_id},
                    )
                    response.raise_for_status()
                    logger.info(
                        "DT transfer ACK artifact=%s source=%s publication=%s",
                        artifact_id,
                        source_endpoint,
                        payload.publication_id,
                    )
        except BaseException:
            # An uncertain read stays registered: dt_release awaits its operation.
            if read_started:
                if payload.transport == "rdma":
                    self.failed(payload.segment)
                await service.release_artifact(artifact_id)
            raise

    async def drain(self) -> None:
        """Join session closes and shared communication owners while their HTTP pool remains open."""
        self.closing = True
        results = await asyncio.gather(
            *(session.close() for session in tuple(self.sessions)),
            *(connection.close() for connection in self.peers.values()),
            return_exceptions=True,
        )
        while self.operations:
            await asyncio.gather(*tuple(self.operations), return_exceptions=True)
        for result in results:
            if isinstance(result, BaseException):
                raise result

    async def close(self) -> None:
        """Release the HTTP pool after RoleService has joined communication and Worker drain."""
        await self.client.aclose()


class DraftPeerSession:
    """Own one remote Draft session and stream; borrow the role's HTTP connection pool."""

    def __init__(self, owner: RoleCommunication, endpoint: str):
        """Retain a request's control handle until its single close owner finishes."""
        self.owner = owner
        self.endpoint = endpoint.rstrip("/")
        self.session_id: str | None = None
        self.response: httpx.Response | None = None
        self.events = None
        self.close_task: asyncio.Task | None = None
        self.tensor_transport: str | None = None
        self.max_candidate_tokens: int | None = None
        self.rdma_connection: RdmaConnection | None = None
        owner.sessions.add(self)

    async def open(self, request, version: int) -> bool:
        """Validate the selected peer and bind its prefix before Target prefill admission."""
        from .service import ROLE_PROTOCOL_VERSION

        response = await self.owner.client.get(
            f"{self.endpoint}/status", params={"observations": "false"}, timeout=None
        )
        response.raise_for_status()
        status = response.json()
        model = self.owner.service.engine.model_config
        metadata = self.owner.service.model_metadata
        expected = {
            "role": "draft",
            "protocol_version": ROLE_PROTOCOL_VERSION,
            "candidate_format": "token_ids_log_probs"
            if self.owner.service.tensor_transports
            else "greedy_token_ids",
            "context_profile": self.owner.service.context_profile,
            "default_draft_tokens": None,
            "tokenizer": metadata.get("tokenizer", model.tokenizer),
            "tokenizer_revision": metadata.get(
                "tokenizer_revision", model.tokenizer_revision
            ),
        }
        if any(status[key] != value for key, value in expected.items()):
            raise ValueError("selected Draft is incompatible with this Target")
        self.tensor_transport = self.owner.select_transport(
            status["tensor_transports"], status["gpu_ipc"]
        )
        if not status["accepting"]:
            return False
        if self.tensor_transport == "rdma":
            self.rdma_connection = self.owner.bind(status, self.endpoint)
        self.max_candidate_tokens = status["max_candidate_tokens"]
        control = self.owner.client.build_request(
            "POST",
            f"{self.endpoint}/sessions",
            json={
                "token_ids": request.token_ids,
                "version": version,
                "sampling": request.sampling.model_dump(),
                "priority": request.priority,
                "tensor_transport": self.tensor_transport,
            },
            timeout=None,
        )
        self.response = await self.owner.client.send(control, stream=True)
        if self.response.status_code == 503:
            refusal = json.loads(await self.response.aread())
            if refusal.get("error", {}).get("code") == "admission_busy":
                return False
        self.response.raise_for_status()
        self.events = self.response.aiter_lines()
        event = json.loads(await anext(self.events))
        if event["event"] == "admission_closed":
            return False
        if event["event"] != "opened":
            raise ValueError("Draft did not open a control session")
        self.session_id = event["session_id"]
        if (
            self.rdma_connection is not None
            and event["rdma"] != self.rdma_connection.peer.model_dump()
        ):
            raise ValueError("Draft RDMA instance changed during session admission")
        logger.info(
            "DT binding request=%s draft=%s target=%s tensor_transport=%s",
            request.request_id,
            self.endpoint,
            request.target_endpoint,
            self.tensor_transport,
        )
        return True

    async def prepare(self, local_endpoint: str) -> None:
        """Borrow shared path readiness without transferring its resource ownership to this session."""
        if self.rdma_connection is not None:
            await self.rdma_connection.prepare(local_endpoint)

    async def commit(self, base_version: int, version: int, tokens: list[int]) -> None:
        """Send the coordinator's confirmed delta and require its exact frontier acknowledgement."""
        response = await self.owner.client.post(
            f"{self.endpoint}/sessions/{self.session_id}/commit",
            json={
                "base_version": base_version,
                "version": version,
                "token_ids": tokens,
            },
            timeout=None,
        )
        response.raise_for_status()
        if response.json()["version"] != version:
            raise ValueError("Draft did not acknowledge the confirmed frontier")

    async def propose(
        self, version: int, budget: int, source: dict, work: dict
    ) -> dict:
        """Request one chain from the confirmed prefix; closed/revoked contexts fail the call."""
        response = await self.owner.client.post(
            f"{self.endpoint}/sessions/{self.session_id}/propose",
            json={
                "version": version,
                "max_tokens": budget,
                "context_source": source,
                "work": work,
            },
            timeout=None,
        )
        response.raise_for_status()
        return response.json()

    async def revoke(self, artifact_id: str) -> None:
        """Revoke an unused peer proposal; its Worker retains any claimed publication until ACK."""
        response = await self.owner.client.delete(
            f"{self.endpoint}/artifacts/{artifact_id}", timeout=None
        )
        response.raise_for_status()

    async def close(self) -> None:
        """Join the single remote close owner before reporting this binding released."""
        with CancelScope(shield=True):
            if self.close_task is None:
                self.close_task = asyncio.create_task(self._close())
            await asyncio.shield(self.close_task)
            self.owner.sessions.discard(self)

    async def _close(self) -> None:
        """Abort the peer's native work and wait for its artifact cleanup acknowledgement."""
        try:
            if self.session_id is not None:
                response = await self.owner.client.delete(
                    f"{self.endpoint}/sessions/{self.session_id}", timeout=None
                )
                response.raise_for_status()
        finally:
            if self.response is not None:
                await self.response.aclose()
