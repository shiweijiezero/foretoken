# SPDX-License-Identifier: Apache-2.0
# SPDX-FileCopyrightText: Copyright contributors to the Foretoken project

"""Target-owned request rounds; each engine retains its native scheduling loop."""

from __future__ import annotations

import json
import logging
import time
from typing import TYPE_CHECKING
from uuid import uuid4

from .connections import DraftPeerSession

if TYPE_CHECKING:
    from .service import Generate, RoleService

logger = logging.getLogger(__name__)


class TargetCoordinator:
    """Own confirmed progress and round decisions for one communication-managed peer binding."""

    def __init__(self, service: RoleService, session_id: str, request: Generate):
        """Track one bound request's confirmed rounds; RoleService joins its close owner."""
        self.service = service
        self.session_id = session_id
        self.request = request
        self.peer = DraftPeerSession(service.communication, request.draft_endpoints[0])
        self.frontier = 0
        self.round = 0
        self.started = time.monotonic()
        self.target_started = self.started
        self.draft_seconds: float | None = None
        self.proposal: dict | None = None
        self.admitted: bool | None = None
        self.requested_tokens: int | None = None
        self.pending_delta: dict | None = None

    def identity(self, phase: str) -> dict:
        """Bind native work to the client request, round and confirmed output frontier."""
        return {
            "request_id": self.request.request_id,
            "work_id": uuid4().hex,
            "frontier": self.frontier,
            "round": self.round,
            "phase": phase,
        }

    async def prepare_binding(self) -> None:
        """Borrow the connection's shared readiness before native prefill.

        Cancelling this request ends only its wait. The communication owner
        retains the verification task, publications and completion/ACK duties.
        """
        await self.peer.prepare(self.request.target_endpoint)
        self.target_started = time.monotonic()

    def confirmed(self, event: dict) -> None:
        """Advance native-confirmed progress and retain its delta for the next proposal call."""
        tokens = event["token_ids"]
        frontier = self.frontier + len(tokens)
        if event["version"] is not None and event["version"] != frontier:
            raise ValueError(
                "Target ticket does not match the confirmed output frontier"
            )
        self.pending_delta = (
            {
                "base_version": self.frontier,
                "version": frontier,
                "token_ids": tokens,
            }
            if tokens and not event["finished"]
            else None
        )
        self.frontier = frontier
        if event["version"] is not None:
            self.peer.prepare_context(
                self.session_id, self.service.targets[self.session_id]
            )
        now = time.monotonic()
        event["round"] = {
            "request_id": self.request.request_id,
            "round": self.round,
            "version": event["version"],
            "phase": "prefill" if self.round == 0 else "verify",
            "outcome": "completed"
            if event["finished"]
            else "preempted"
            if event["observation"]["preemptions"] > 0
            else "progress",
            "draft_endpoint": self.peer.endpoint,
            "target_endpoint": self.request.target_endpoint,
            "tensor_transport": self.peer.tensor_transport,
            "context_tokens": len(self.request.token_ids) + frontier,
            "batch_requests": event["observation"]["batch_requests"],
            "elapsed_seconds": now - self.started,
            "draft_seconds": self.draft_seconds,
            "target_seconds": now - self.target_started,
            "proposed_tokens": len(self.proposal["token_ids"])
            if self.proposal is not None
            else None,
            "requested_tokens": self.requested_tokens,
            "committed_tokens": len(tokens),
            "admitted": self.admitted,
            "context_transfer": self.proposal["context_transfer"]
            if self.proposal is not None
            else None,
            "draft": self.proposal["observation"]
            if self.proposal is not None
            else None,
            "target": event["observation"],
            "transfer": event["transfer"],
        }
        logger.info("DT round %s", json.dumps(event["round"]))

    def select_candidate_count(self) -> int:
        """Choose this round's K; advance owns execution and the native Scheduler owns batching.

        The default uses the configured Target policy, bounded by remaining output
        and the bound method/engine capacities. Future K policies belong here and
        must return a count within these same execution limits.
        """
        capacity = self.service.max_candidate_tokens
        if self.peer.max_candidate_tokens is not None:
            capacity = min(capacity, self.peer.max_candidate_tokens)
        return min(
            self.service.default_draft_tokens,
            self.request.max_tokens - self.frontier,
            capacity,
        )

    async def advance(self, ticket) -> None:
        """Request one chain and submit it when ready; native scheduling chooses its forward."""
        from .service import Verify

        if self.session_id not in self.service.targets:
            return
        self.round += 1
        self.started = time.monotonic()
        self.proposal = None
        self.admitted = None
        budget = self.select_candidate_count()
        self.requested_tokens = budget
        # Prefix synchronization and the next proposal share one request. An
        # ambiguous response fails the binding; the delta is never replayed.
        confirmed_delta, self.pending_delta = self.pending_delta, None
        self.proposal = await self.peer.propose(
            self.frontier,
            budget,
            {"endpoint": self.request.target_endpoint, "session_id": self.session_id},
            self.identity("draft"),
            confirmed_delta,
        )
        self.draft_seconds = time.monotonic() - self.started
        if self.proposal["version"] != ticket.generation:
            raise ValueError("proposal belongs to a different confirmed frontier")
        if self.session_id not in self.service.targets:
            return
        self.target_started = time.monotonic()
        self.admitted = await self.service.verify(
            self.session_id,
            Verify(
                version=ticket.generation,
                token_ids=self.proposal["token_ids"],
                artifact=self.proposal["artifact"],
                source_endpoint=self.peer.endpoint,
            ),
            self.identity("verify"),
        )
        if not self.admitted and self.proposal["artifact"] is not None:
            artifact_id = self.proposal["artifact"]["artifact_id"]
            await self.peer.revoke(artifact_id)
        if not self.admitted and self.session_id in self.service.targets:
            # KV eviction preserves the ticket. A live rejection has no replay
            # output to wait for; surface the broken round instead of hanging.
            raise RuntimeError("Target rejected the outstanding round ticket")
