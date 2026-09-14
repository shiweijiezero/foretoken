# SPDX-License-Identifier: Apache-2.0
# SPDX-FileCopyrightText: Copyright contributors to the Foretoken project

"""Read-only prefix lookup through the active vLLM Mooncake Store worker."""

import os
import threading

import zmq
from vllm.distributed.kv_transfer.kv_connector.v1.mooncake.store.connector import (
    MooncakeStoreConnector as UpstreamMooncakeStoreConnector,
)
from vllm.sampling_params import SamplingParams
from vllm.utils.hashing import get_hash_fn_by_name
from vllm.v1.core.kv_cache_utils import get_request_block_hasher, init_none_hash
from vllm.v1.request import Request


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


class _LookupView:
    """Use upstream key construction and hit alignment without replacing its Store handle."""

    def __init__(self, worker):
        self.worker = worker
        self.store = _CheckedStore(worker.store)

    def __getattr__(self, name):
        return getattr(self.worker, name)


class _PrefixServer:
    """Own the local query socket until connector shutdown; no KV data is transferred."""

    def __init__(self, worker, config):
        self.worker = worker
        hash_fn = get_hash_fn_by_name(config.cache_config.prefix_caching_hash_algo)
        # Native scheduler hashing uses this same process-independent seed.
        if "PYTHONHASHSEED" not in os.environ:
            raise ValueError("shared prefix lookup requires PYTHONHASHSEED")
        init_none_hash(hash_fn)
        self.block_hasher = get_request_block_hasher(worker.hash_block_size, hash_fn)
        self.stopping = threading.Event()
        self.context = zmq.Context()
        self.endpoint = os.environ["FORETOKEN_SHARED_KV_LOOKUP_ENDPOINT"]
        self.thread = threading.Thread(target=self._serve, daemon=True)
        self.thread.start()

    def _serve(self):
        with self.context.socket(zmq.REP) as socket:
            socket.setsockopt(zmq.LINGER, 0)
            socket.bind(self.endpoint)
            while not self.stopping.is_set():
                if not socket.poll(100):
                    continue
                tokens = socket.recv_json()["promptTokenIds"]
                request = Request(
                    request_id="prefix-observation",
                    prompt_token_ids=tokens,
                    sampling_params=SamplingParams(max_tokens=1),
                    pooling_params=None,
                    block_hasher=self.block_hasher,
                )
                # Match the native scheduler's complete-block lookup boundary.
                block_size = self.worker.coord.lcm_block_size
                token_len = len(tokens) // block_size * block_size
                view = _LookupView(self.worker)
                matched = type(self.worker).lookup(view, token_len, request.block_hashes)
                socket.send_json({
                    "matchedTokens": None if view.store.failed else matched,
                    "blockSize": block_size,
                })

    def close(self):
        self.stopping.set()
        self.thread.join()
        self.context.term()


class MooncakeStoreConnector(UpstreamMooncakeStoreConnector):
    """Extend the native connector with a Pod-local, read-only prefix observation socket."""

    def __init__(self, vllm_config, role, kv_cache_config=None):
        self._prefix_server = None
        self._config = vllm_config
        # Reuse the controller's model/layout scope for both transfers and observations.
        vllm_config.kv_transfer_config.kv_connector_extra_config.setdefault(
            "cache_prefix", os.environ["FORETOKEN_KV_SCOPE_ID"]
        )
        super().__init__(vllm_config, role, kv_cache_config)

    def _start_prefix_server(self):
        if self._config.parallel_config.rank == 0 and self._prefix_server is None:
            self._prefix_server = _PrefixServer(self.connector_worker, self._config)

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
