# SPDX-License-Identifier: Apache-2.0
# SPDX-FileCopyrightText: Copyright contributors to the Foretoken project

"""Expose retained Decode prefixes through vLLM's native Mooncake transfers."""

from vllm.distributed.kv_transfer.kv_connector.v1.base import KVConnectorRole
from vllm.distributed.kv_transfer.kv_connector.v1.mooncake.mooncake_connector import (
    MooncakeConnector as UpstreamMooncakeConnector,
)
from vllm.v1.kv_cache_interface import is_full_attention_spec

from ..retained import RetainedPrefixConnector


class MooncakeConnector(RetainedPrefixConnector, UpstreamMooncakeConnector):
    """Share prefix discovery while native Mooncake retains and transfers KV blocks."""

    def __init__(self, vllm_config, role, kv_cache_config):
        transfer = vllm_config.kv_transfer_config
        bidirectional = bool(
            transfer.get_from_extra_config("bidirectional_kv_xfer", False)
            and all(
                is_full_attention_spec(group.kv_cache_spec)
                for group in kv_cache_config.transfer_groups
            )
        )
        transfer.kv_connector_extra_config["bidirectional_kv_xfer"] = bidirectional
        super().__init__(vllm_config, role, kv_cache_config)
        self._initialize_retained_prefixes(
            vllm_config,
            bidirectional,
            role == KVConnectorRole.SCHEDULER
            and transfer.kv_role == "kv_consumer"
            and bidirectional,
        )

    def cancel_retained_source(self, *args):
        """Revoke sources once native workers confirm all admitted writes have stopped."""
        return self.connector_worker.cancel_send_lease(*args)
