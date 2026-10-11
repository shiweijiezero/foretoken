# SPDX-License-Identifier: Apache-2.0
# SPDX-FileCopyrightText: Copyright contributors to the Foretoken project

"""Round-boundary KV checkpoints loaded through vLLM's native block allocator."""

from dataclasses import dataclass
from math import ceil

import torch
from vllm.distributed.kv_transfer.kv_connector.v1.base import (
    KVConnectorBase_V1,
    KVConnectorMetadata,
)
from vllm.v1.kv_cache_interface import FullAttentionSpec


@dataclass
class CheckpointLoads(KVConnectorMetadata):
    """Device checkpoints and native destination blocks for one scheduler step."""

    loads: list[tuple[str, list[int], int, int]]


class DraftTargetKvConnector(KVConnectorBase_V1):
    """Import received checkpoints; vLLM retains allocation, cache and preemption ownership."""

    def __init__(self, vllm_config, role, kv_cache_config):
        super().__init__(vllm_config, role, kv_cache_config)
        groups = kv_cache_config.kv_cache_groups
        if len(groups) != 1 or type(groups[0].kv_cache_spec) is not FullAttentionSpec:
            raise ValueError("DT migration requires one full-attention KV group")
        self.block_size = groups[0].kv_cache_spec.block_size
        self.loads = []
        self.caches = {}
        self.io = None

    @property
    def requires_kv_delivery(self):
        """Source checkpoints own copies; native request pages never wait for a reader."""
        return False

    def register_kv_caches(self, kv_caches):
        """Retain standardized per-layer [blocks, heads, tokens, channels] views."""
        if any(
            cache.ndim != 4 or cache.shape[2] != self.block_size
            for cache in kv_caches.values()
        ):
            raise ValueError("DT migration requires unsplit full-attention layer views")
        self.caches = dict(sorted(kv_caches.items()))

    def layout(self):
        """Describe actual layer views so the receiver can reject incompatible checkpoints."""
        return [
            {"layer": name, "shape": list(cache.shape[1:]), "dtype": str(cache.dtype)}
            for name, cache in self.caches.items()
        ]

    def snapshot(self, artifact_id, block_ids):
        """Copy live KV on the Worker stream before EngineCore can preempt its pages."""
        indices = torch.tensor(
            block_ids, device=next(iter(self.caches.values())).device
        )
        parts = [
            cache.index_select(0, indices).contiguous().view(torch.uint8).flatten()
            for cache in self.caches.values()
        ]
        self.io.publish_tensor(artifact_id, torch.cat(parts))
        return self.layout()

    def get_num_new_matched_tokens(self, request, num_computed_tokens):
        """Report only received KV, allowing native prefix hits to take precedence."""
        checkpoint = request.kv_transfer_params
        if not checkpoint:
            return 0, False
        checkpoint["local_tokens"] = num_computed_tokens
        return max(checkpoint["computed_tokens"] - num_computed_tokens, 0), False

    def update_state_after_alloc(self, request, blocks, num_external_tokens):
        """Bind received pages to the allocation chosen by the native scheduler."""
        checkpoint = request.kv_transfer_params
        if checkpoint is None:
            return
        if num_external_tokens:
            computed = checkpoint["computed_tokens"]
            self.loads.append(
                (
                    checkpoint["artifact_id"],
                    blocks.get_block_ids()[0],
                    computed,
                    checkpoint["local_tokens"] // self.block_size,
                )
            )
        # A preempted request subsequently uses native prefix caching/recomputation.
        request.kv_transfer_params = None

    def build_connector_meta(self, scheduler_output):
        """Transfer this step's imports to the Worker before its first forward."""
        metadata = CheckpointLoads(self.loads)
        self.loads = []
        return metadata

    def start_load_kv(self, forward_context, **kwargs):
        """Scatter checkpoint pages on the compute stream before attention reads them."""
        for (
            artifact_id,
            block_ids,
            computed,
            local_blocks,
        ) in self._get_connector_metadata().loads:
            artifact = self.io.artifacts[artifact_id]
            artifact.operation.result()
            source = artifact.buffer.tensor
            count = ceil(computed / self.block_size)
            offset = 0
            for cache in self.caches.values():
                size = count * cache[0].numel() * cache.element_size()
                pages = (
                    source[offset : offset + size]
                    .view(cache.dtype)
                    .reshape(count, *cache.shape[1:])
                )
                indices = torch.tensor(
                    block_ids[local_blocks:count], device=cache.device
                )
                cache.index_copy_(0, indices, pages[local_blocks:])
                offset += size
            if offset != source.numel():
                raise ValueError("KV checkpoint does not match destination layout")
            artifact.event = torch.cuda.Event()
            artifact.event.record(torch.cuda.current_stream(source.device))

    def wait_for_layer_load(self, layer_name):
        """Imports precede the forward on the same compute stream."""

    def save_kv_layer(self, layer_name, kv_layer, attn_metadata, **kwargs):
        """Checkpoints are explicitly exported between rounds, not during attention."""

    def wait_for_save(self):
        """The connector has no background saves of native KV pages."""
