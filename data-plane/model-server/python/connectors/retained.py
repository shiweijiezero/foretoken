# SPDX-License-Identifier: Apache-2.0
# SPDX-FileCopyrightText: Copyright contributors to the Foretoken project

"""Share retained-prefix discovery and acquisition across native P/D connectors."""

from __future__ import annotations

import threading
import time
import uuid
from dataclasses import dataclass, field
from typing import Any

from vllm.distributed.kv_transfer import get_kv_transfer_group


@dataclass
class _RetainedPrefix:
    request_id: str
    token_ids: tuple[int, ...]
    cache_salt: str | None
    descriptor: dict[str, Any]
    expires_at: float
    consumer_id: str | None = None
    authorized_readers: set[int] = field(default_factory=set)

    def matches(self, payload: dict[str, Any]) -> bool:
        return self.cache_salt == payload.get("cache_salt") and self.token_ids == tuple(
            payload["prompt_token_ids"][: len(self.token_ids)]
        )


class RetainedPrefixConnector:
    """Own a token directory while the native connector owns references and I/O."""

    def _initialize_retained_prefixes(self, vllm_config, enabled, retain_sources):
        """Bind this directory to one engine instance before accepting utility calls."""
        self._instance_id = vllm_config.instance_id
        self._dp_rank = vllm_config.parallel_config.data_parallel_index
        self._bidirectional_kv_xfer = enabled
        self._prefixes: dict[str, _RetainedPrefix] = {}
        self._prefix_lock = threading.Lock()
        self._accepting = True
        self._retain_prefixes = retain_sources

    def request_finished(self, request, block_ids):
        """Record eligible prefixes from the scheduler's native completion hook."""
        return self._record_prefix(
            request, super().request_finished(request, block_ids)
        )

    def request_finished_all_groups(self, request, block_ids):
        """Record eligible prefixes across cache groups at scheduler completion."""
        return self._record_prefix(
            request, super().request_finished_all_groups(request, block_ids)
        )

    def handle_utility_call(self, operation, payload, worker_rpc):
        """Serve EngineCore utilities; grant a source only after all workers pin it."""
        if operation == "configuration":
            return {
                "connector": self._kv_transfer_config.kv_connector,
                "bidirectional_kv_xfer": self._bidirectional_kv_xfer,
            }
        result = {"dp_rank": self._dp_rank, "instance_id": self._instance_id}
        with self._prefix_lock:
            self._expire_available()
            if operation == "retained_prefix":
                matches = [
                    (offer_id, prefix)
                    for offer_id, prefix in self._prefixes.items()
                    if self._accepting
                    and prefix.consumer_id is None
                    and prefix.matches(payload)
                ]
                match = max(
                    matches, key=lambda item: len(item[1].token_ids), default=None
                )
                result["offer"] = (
                    {"id": match[0], "matched_tokens": len(match[1].token_ids)}
                    if match is not None
                    else None
                )
                return result
            if operation == "drain":
                self._accepting = False
                self._prefixes = {
                    offer_id: prefix
                    for offer_id, prefix in self._prefixes.items()
                    if prefix.consumer_id is not None
                }
                worker_pending = worker_rpc(
                    _worker_lease_call, args=("pending_transfers",)
                )
                pending = self.connector_scheduler.get_pending_transfers().union(
                    *worker_pending,
                    (prefix.request_id for prefix in self._prefixes.values()),
                )
                result["active_leases"] = len(pending)
                return result
            if operation not in {"claim", "cancel"}:
                raise ValueError(f"Unknown KV utility operation: {operation}")
            if (
                payload["dp_rank"] != self._dp_rank
                or payload["instance_id"] != self._instance_id
            ):
                return {**result, "status": "Miss"}
            offer_id = payload["offer_id"]
            prefix = self._prefixes.get(offer_id)
            consumer_id = payload["consumer_id"]
            if prefix is None:
                return {
                    **result,
                    "status": "Released" if operation == "cancel" else "Miss",
                }
            if operation == "cancel":
                if prefix.consumer_id not in (None, consumer_id):
                    return {**result, "status": "Miss"}
                if prefix.authorized_readers:
                    return {**result, "status": "InUse"}
                # Native workers serialize revocation with actual transfer submission.
                # An active worker keeps the source until the native completion barrier.
                if prefix.consumer_id is not None:
                    revoked = worker_rpc(
                        _worker_lease_call,
                        args=("cancel", prefix.request_id, offer_id, consumer_id),
                    )
                    if not all(revoked):
                        return {**result, "status": "InUse"}
                del self._prefixes[offer_id]
                return {**result, "status": "Released"}
            if (
                not self._accepting
                or prefix.consumer_id is not None
                or not prefix.matches(payload)
            ):
                return {**result, "status": "Miss"}
            # Preserve claim identity so RPC failures can still be cleaned up.
            prefix.consumer_id = consumer_id
            pinned = worker_rpc(
                _worker_lease_call,
                args=("claim", prefix.request_id, offer_id, consumer_id),
            )
            if not all(pinned):
                worker_rpc(
                    _worker_lease_call,
                    args=("cancel", prefix.request_id, offer_id, consumer_id),
                )
                del self._prefixes[offer_id]
                return {**result, "status": "Miss"}
            descriptor = {
                **prefix.descriptor,
                "remote_read_lease": offer_id,
                "remote_consumer_id": consumer_id,
            }
            return {**result, "status": "Granted", "descriptor": descriptor}

    def update_connector_output(self, connector_output):
        """Remove completed sources before the scheduler releases their KV references."""
        super().update_connector_output(connector_output)
        finished = connector_output.finished_sending or ()
        if finished:
            with self._prefix_lock:
                self._prefixes = {
                    offer_id: prefix
                    for offer_id, prefix in self._prefixes.items()
                    if prefix.request_id not in finished
                }

    def _record_prefix(self, request, result):
        """Publish text prefixes only after the native connector retains their blocks."""
        delay_free, descriptor = result
        if not self._retain_prefixes:
            return result
        if (
            not delay_free
            or descriptor is None
            or not descriptor.get("do_remote_decode")
            or request.lora_request is not None
            or request.mm_features
            or request.prompt_embeds is not None
        ):
            return delay_free, None
        num_tokens = descriptor["remote_num_tokens"]
        if not 0 < num_tokens <= len(request.all_token_ids):
            return delay_free, None
        prefix = _RetainedPrefix(
            request_id=request.request_id,
            token_ids=tuple(request.all_token_ids[:num_tokens]),
            cache_salt=request.cache_salt,
            descriptor=dict(descriptor),
            expires_at=descriptor["remote_blocks_expiry_time"],
        )
        with self._prefix_lock:
            if self._accepting:
                self._expire_available()
                self._prefixes[uuid.uuid4().hex] = prefix
        # Decode descriptors are owned by this directory, not the output consumer.
        return delay_free, None

    def _expire_available(self):
        now = time.perf_counter()
        self._prefixes = {
            offer_id: prefix
            for offer_id, prefix in self._prefixes.items()
            if prefix.consumer_id is not None or now < prefix.expires_at
        }

    def _authorize_read(self, request_id, lease_id, consumer_id, reader_rank):
        """Authorize pull readers under the same lock used to revoke unstarted reads."""
        with self._prefix_lock:
            prefix = self._prefixes.get(lease_id)
            if (
                prefix is None
                or prefix.request_id != request_id
                or prefix.consumer_id is None
                or prefix.consumer_id != consumer_id
                or reader_rank in prefix.authorized_readers
            ):
                return False
            prefix.authorized_readers.add(reader_rank)
            return True

    def _read_lease_released(self, lease_id):
        """Observe source release without changing native references or completions."""
        with self._prefix_lock:
            return lease_id not in self._prefixes


def _worker_lease_call(worker, operation: str, *args):
    """Run connector utilities on each worker through the executor's RPC."""
    connector = get_kv_transfer_group()
    assert isinstance(connector, RetainedPrefixConnector)
    native = connector.connector_worker
    assert native is not None
    if operation == "claim":
        return native.claim_send_lease(*args)
    if operation == "cancel":
        return connector.cancel_retained_source(*args)
    if operation == "pending_transfers":
        return native.get_pending_transfers()
    raise ValueError(f"Unknown KV worker operation: {operation}")
