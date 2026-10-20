# SPDX-License-Identifier: Apache-2.0
# SPDX-FileCopyrightText: Copyright contributors to the Foretoken project

"""Read-only prefix lookup through the active vLLM Mooncake Store worker."""

import logging
import os

from foretoken_runtime import PrefixLookupServer, offset_tcp_endpoint
from vllm.distributed.kv_transfer.kv_connector.v1.mooncake.store.connector import (
    MooncakeStoreConnector as UpstreamMooncakeStoreConnector,
)
from vllm.utils.hashing import get_hash_fn_by_name
from vllm.v1.core.kv_cache_utils import get_request_block_hasher, init_none_hash
from vllm.v1.request import Request

logger = logging.getLogger(__name__)


class MooncakeStoreConnector(UpstreamMooncakeStoreConnector):
    """Expose read-only prefix observations from each DP rank's native connector."""

    def __init__(self, vllm_config, role, kv_cache_config=None):
        self._prefix_server = None
        self._config = vllm_config
        # Reuse the controller's model/layout scope for both transfers and observations.
        vllm_config.kv_transfer_config.kv_connector_extra_config.setdefault(
            "cache_prefix", os.environ["FORETOKEN_KV_SCOPE_ID"]
        )
        super().__init__(vllm_config, role, kv_cache_config)

    def register_kv_caches(self, kv_caches):
        super().register_kv_caches(kv_caches)
        self._start_prefix_server()

    def register_cross_layers_kv_cache(self, kv_cache, attn_backend):
        super().register_cross_layers_kv_cache(kv_cache, attn_backend)
        self._start_prefix_server()

    def shutdown(self):
        if self._prefix_server is not None:
            self._prefix_server.close()
            self._prefix_server = None
        super().shutdown()

    def _start_prefix_server(self):
        if self._config.parallel_config.rank == 0 and self._prefix_server is None:
            self._prefix_server = _PrefixServer(self.connector_worker, self._config)


class _PrefixServer:
    """Expose one DP rank's native connector through the shared socket lifecycle."""

    def __init__(self, worker, config):
        self.worker = worker
        hash_fn = get_hash_fn_by_name(config.cache_config.prefix_caching_hash_algo)
        # Native scheduler hashing uses this same process-independent seed.
        if "PYTHONHASHSEED" not in os.environ:
            raise ValueError("shared prefix lookup requires PYTHONHASHSEED")
        init_none_hash(hash_fn)
        self.block_hasher = get_request_block_hasher(worker.hash_block_size, hash_fn)
        self.dp_rank = config.parallel_config.data_parallel_index
        endpoint = offset_tcp_endpoint(
            os.environ["FORETOKEN_SHARED_KV_LOOKUP_ENDPOINT"], self.dp_rank
        )
        self.server = PrefixLookupServer(endpoint, self._lookup)

    def close(self):
        self.server.close()

    def _lookup(self, query):
        """Observe the native Store's reusable prefix without scheduling a load."""
        if query.get("dpRank") != self.dp_rank:
            return {"matchedTokens": None}
        try:
            tokens = query["promptTokenIds"]
            request = Request.for_prefix_cache_lookup(
                prompt_token_ids=tokens,
                block_hasher=self.block_hasher,
                cache_salt=query.get("cacheSalt"),
                lora_name=query.get("loraName"),
                mm_features=query.get("multimodal"),
                lora_path=query.get("loraPath"),
            )
            # Native matching owns group convergence and partial-block hit boundaries.
            view = _LookupView(self.worker)
            matched = type(self.worker).lookup_prefix(
                view, len(tokens), request.block_hashes
            )
            return {"matchedTokens": None if view.store.failed else matched}
        except Exception:
            logger.exception("vLLM shared KV prefix lookup failed")
            return {"matchedTokens": None}


class _LookupView:
    """Use upstream key construction and hit alignment without replacing its Store handle."""

    def __init__(self, worker):
        self.worker = worker
        self.store = _CheckedStore(worker.store)

    def __getattr__(self, name):
        return getattr(self.worker, name)


class _CheckedStore:
    """Preserve native lookup failures that the connector otherwise treats as misses."""

    def __init__(self, store):
        self.store = store
        self.failed = False

    def batch_is_exist(self, keys):
        self.failed = True
        results = self.store.batch_is_exist(keys)
        self.failed = len(results) != len(keys) or any(value < 0 for value in results)
        return results
