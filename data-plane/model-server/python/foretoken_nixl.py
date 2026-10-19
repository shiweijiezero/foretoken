# SPDX-License-Identifier: Apache-2.0
# SPDX-FileCopyrightText: Copyright contributors to the Foretoken project

"""通过原生 NIXL 引用为 model-server 提供精确前缀查询与单次领取。"""

from __future__ import annotations

import threading
import time
import uuid
from dataclasses import dataclass, field
from typing import Any

from vllm.distributed.kv_transfer import get_kv_transfer_group
from vllm.distributed.kv_transfer.kv_connector.v1.base import KVConnectorRole
from vllm.distributed.kv_transfer.kv_connector.v1.nixl.connector import (
    NixlConnector as UpstreamNixlConnector,
)
from vllm.platforms import current_platform
from vllm.v1.kv_cache_interface import is_full_attention_spec


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


def _worker_lease_call(worker, operation: str, *args):
    """由 executor 在各 worker 的执行线程调用，返回原生块所有者的处理结果。"""
    connector = get_kv_transfer_group()
    assert isinstance(connector, NixlConnector)
    native = connector.connector_worker
    assert native is not None
    if operation == "claim":
        return native.claim_send_lease(*args)
    if operation == "cancel":
        native.cancel_send_lease(*args)
        return None
    if operation == "pending_transfers":
        return native.get_pending_transfers()
    raise ValueError(f"Unknown NIXL worker operation: {operation}")


class NixlConnector(UpstreamNixlConnector):
    """在 scheduler 保存来源身份，块保留和 READ 完成仍归原生 connector。"""

    def __init__(self, vllm_config, role, kv_cache_config):
        super().__init__(vllm_config, role, kv_cache_config)
        native = (
            self.connector_scheduler
            if role == KVConnectorRole.SCHEDULER
            else self.connector_worker
        )
        assert native is not None
        # 实际缓存布局在此已确定；请求与 KV 注册尚未开始，两端统一收敛原生值。
        bidirectional = bool(
            self.kv_transfer_config.get_from_extra_config(
                "bidirectional_kv_xfer", False
            )
            and current_platform.is_cuda()
            and not native.use_host_buffer
            and all(
                is_full_attention_spec(group.kv_cache_spec)
                for group in kv_cache_config.transfer_groups
            )
        )
        self.kv_transfer_config.kv_connector_extra_config["bidirectional_kv_xfer"] = (
            bidirectional
        )
        if role == KVConnectorRole.SCHEDULER:
            native.is_bidirectional_kv_xfer_enabled = bidirectional
        else:
            native._bidirectional_kv_xfer_enabled = bidirectional
        self._instance_id = vllm_config.instance_id
        self._dp_rank = vllm_config.parallel_config.data_parallel_index
        self._prefixes: dict[str, _RetainedPrefix] = {}
        self._prefix_lock = threading.Lock()
        self._accepting = True
        self._retain_prefixes = (
            role == KVConnectorRole.SCHEDULER
            and self.kv_transfer_config.kv_role == "kv_consumer"
            and bidirectional
        )
        if role == KVConnectorRole.SCHEDULER:
            self.connector_scheduler.read_lease_handler = self._authorize_read
            self.connector_scheduler.read_lease_state_handler = (
                self._read_lease_released
            )

    def request_finished(self, request, block_ids):
        """在 scheduler 确认延迟释放后登记原始 token，返回原生完成结果。"""
        return self._record_prefix(
            request, super().request_finished(request, block_ids)
        )

    def request_finished_all_groups(self, request, block_ids):
        """为多 cache group 的 scheduler 完成钩子登记同一来源生命周期。"""
        return self._record_prefix(
            request, super().request_finished_all_groups(request, block_ids)
        )

    def _record_prefix(self, request, result):
        """只发布普通文本 D 请求已被原生引用保留的完整描述符范围。"""
        delay_free, descriptor = result
        if (
            not self._retain_prefixes
            or not delay_free
            or descriptor is None
            or not descriptor.get("do_remote_decode")
            or request.lora_request is not None
            or request.mm_features
            or request.prompt_embeds is not None
        ):
            return result
        num_tokens = descriptor["remote_num_tokens"]
        if not 0 < num_tokens <= len(request.all_token_ids):
            return result
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
        return result

    def update_connector_output(self, connector_output):
        """在全部源 worker 完成后移除目录；scheduler 随后释放原生块引用。"""
        super().update_connector_output(connector_output)
        finished = connector_output.finished_sending or ()
        if finished:
            with self._prefix_lock:
                self._prefixes = {
                    offer_id: prefix
                    for offer_id, prefix in self._prefixes.items()
                    if prefix.request_id not in finished
                }

    def _expire_available(self):
        now = time.perf_counter()
        self._prefixes = {
            offer_id: prefix
            for offer_id, prefix in self._prefixes.items()
            if prefix.consumer_id is not None or now < prefix.expires_at
        }

    def _authorize_read(self, request_id, lease_id, consumer_id, reader_rank):
        """在 side-channel 线程原子关闭撤销窗口，不在此线程操作 GPU worker。"""
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
        """只观察原生全 worker 完成后移除的来源，不改变引用或完成计数。"""
        with self._prefix_lock:
            return lease_id not in self._prefixes

    def handle_utility_call(self, operation, payload, worker_rpc):
        """响应 EngineCore utility；仅在全部 native worker 确认后返回 Granted。"""
        if operation == "configuration":
            return {
                "connector": "NixlConnector",
                "bidirectional_kv_xfer": (
                    self.connector_scheduler.is_bidirectional_kv_xfer_enabled
                ),
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
                raise ValueError(f"Unknown NIXL utility operation: {operation}")
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
                # 与读授权共用锁，因此 worker 解 pin 期间不能有新的 READ 获准。
                if prefix.consumer_id is not None:
                    worker_rpc(
                        _worker_lease_call,
                        args=("cancel", prefix.request_id, offer_id, consumer_id),
                    )
                del self._prefixes[offer_id]
                return {**result, "status": "Released"}
            if (
                not self._accepting
                or prefix.consumer_id is not None
                or not prefix.matches(payload)
            ):
                return {**result, "status": "Miss"}
            # RPC 异常也保留领取身份，执行清理仍能撤销已成功保留的 worker。
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
            return {
                **result,
                "status": "Granted",
                "descriptor": descriptor,
            }
