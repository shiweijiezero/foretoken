# SPDX-License-Identifier: Apache-2.0
# SPDX-FileCopyrightText: Copyright contributors to the Foretoken project

"""Bind retained-prefix discovery to vLLM's native NIXL lifecycle."""

from vllm.distributed.kv_transfer.kv_connector.v1.base import KVConnectorRole
from vllm.distributed.kv_transfer.kv_connector.v1.nixl.connector import (
    NixlConnector as UpstreamNixlConnector,
)
from vllm.v1.kv_cache_interface import is_full_attention_spec

from ..retained import RetainedPrefixConnector


class NixlConnector(RetainedPrefixConnector, UpstreamNixlConnector):
    """Track source identities while native NIXL owns KV retention and transfer."""

    def __init__(self, vllm_config, role, kv_cache_config):
        super().__init__(vllm_config, role, kv_cache_config)
        native = (
            self.connector_scheduler
            if role == KVConnectorRole.SCHEDULER
            else self.connector_worker
        )
        assert native is not None
        # Finalize bidirectional settings before cache registration and admission.
        bidirectional = bool(
            self.kv_transfer_config.get_from_extra_config(
                "bidirectional_kv_xfer", False
            )
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
        self._initialize_retained_prefixes(
            vllm_config,
            bidirectional,
            role == KVConnectorRole.SCHEDULER
            and self.kv_transfer_config.kv_role == "kv_consumer"
            and bidirectional,
        )

    def _claim_source(self, request_id, lease_id, consumer_id, worker_rpc):
        """Claim references already retained by the native source scheduler."""
        return self.connector_scheduler.claim_send_lease(
            request_id, lease_id, consumer_id
        )

    def _cancel_source(self, request_id, lease_id, consumer_id, worker_rpc):
        """Revoke through the same native owner that authorizes both READ directions."""
        return self.connector_scheduler.cancel_send_lease(
            request_id, lease_id, consumer_id
        )
