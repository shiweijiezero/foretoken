# SPDX-License-Identifier: Apache-2.0
# SPDX-FileCopyrightText: Copyright contributors to the Foretoken project

"""Execute native EAGLE proposals with an independent model and KV pool."""

import gc
import logging
import time
from dataclasses import dataclass
from typing import Any, cast

import numpy as np
import torch
from torch import nn
from vllm.config import VllmConfig, set_current_vllm_config
from vllm.config.compilation import CUDAGraphMode
from vllm.distributed.parallel_state import (
    destroy_distributed_environment,
    destroy_model_parallel,
)
from vllm.forward_context import set_forward_context
from vllm.model_executor.layers.vocab_parallel_embedding import (
    ParallelLMHead,
    VocabParallelEmbedding,
)
from vllm.platforms import current_platform
from vllm.utils.math_utils import cdiv
from vllm.utils.network_utils import get_distributed_init_method, get_open_port
from vllm.v1.core.kv_cache_manager import KVCacheManager
from vllm.v1.core.kv_cache_utils import (
    get_kv_cache_configs,
    resolve_kv_cache_block_sizes,
)
from vllm.v1.request import Request, RequestStatus
from vllm.v1.worker.block_table import get_block_table_width
from vllm.v1.worker.gpu.attn_utils import (
    build_slot_mappings_by_layer,
    get_kv_cache_spec,
    init_attn_backend,
    init_kv_cache,
)
from vllm.v1.worker.gpu.block_table import BlockTables
from vllm.v1.worker.gpu.buffer_utils import async_copy_to_gpu
from vllm.v1.worker.gpu.input_batch import InputBatch, InputBuffers
from vllm.v1.worker.gpu.model_states.default import DefaultModelState
from vllm.v1.worker.gpu.spec_decode.eagle.speculator import EagleSpeculator
from vllm.v1.worker.gpu_worker import init_worker_distributed_environment
from vllm.v1.worker.utils import get_uniform_decode_token_count
from vllm.v1.worker.workspace import init_workspace_manager


@dataclass
class _KVRequest:
    """The request fields consumed by vLLM's uncached KV allocation path."""

    request_id: str
    num_prompt_tokens: int
    num_computed_tokens: int = 0
    num_in_flight_tokens: int = 0
    status: RequestStatus = RequestStatus.RUNNING


@dataclass
class _Session:
    """Keep a logical request's slot and Target-provided allocation cursor together."""

    slot: int
    request: _KVRequest


@dataclass
class _DecodeLayout:
    """Retain row/index buffers for an unchanged full-width decode batch."""

    sessions: tuple[_Session, ...]
    shape: tuple[int, int, int, int]
    idx: torch.Tensor
    idx_np: np.ndarray
    query_np: np.ndarray
    logits_indices: torch.Tensor
    blocks: tuple[torch.Tensor, ...] | None = None


class _StagedEagleSpeculator(EagleSpeculator):
    """Fence the three sampler outputs at native EAGLE's first feedback consumer."""

    pending_feedback: (
        tuple[torch.cuda.Event, torch.Tensor, slice | torch.Tensor, torch.Tensor] | None
    ) = None

    def _copy_request_inputs(self, num_reqs, idx_mapping, temperature, seeds) -> None:
        """Finish feature projection and per-request copies before waiting for sampling.

        In the pinned native proposer this hook immediately precedes
        prepare_prefill_inputs, the first reader of sampled/rejected counts.
        """
        super()._copy_request_inputs(num_reqs, idx_mapping, temperature, seeds)
        if self.pending_feedback is not None:
            ready, destination, rows, source = self.pending_feedback
            torch.cuda.current_stream(destination.device).wait_event(ready)
            destination[rows] = source
            self.pending_feedback = None


class DraftRuntime:
    """Own one GPU's EAGLE model, CUDA graphs, slots and native paged KV allocator.

    The Draft server creates this object once and calls ``bootstrap`` before
    proposals. Only EAGLE weights and the vocabulary weights received from
    Target are materialized; no Target transformer layers are constructed.
    """

    def __init__(
        self,
        vllm_config: VllmConfig,
        device: str = "cuda:0",
        kv_cache_memory_bytes: int | None = None,
    ) -> None:
        self.config = vllm_config
        self.device = torch.device(device)
        self.kv_cache_memory_bytes = kv_cache_memory_bytes
        self.speculator: _StagedEagleSpeculator | None = None
        self.kv_manager: KVCacheManager | None = None
        self.kv_caches: list[torch.Tensor] = []
        self.sessions: dict[str, _Session] = {}
        self.decode_layout: _DecodeLayout | None = None
        self.distributed_initialized = False
        self.closed = False
        spec = vllm_config.speculative_config
        if spec is None or spec.method not in ("eagle", "eagle3"):
            raise ValueError("DraftRuntime requires EAGLE or EAGLE3")
        if spec.draft_sample_method != "greedy":
            raise ValueError("DraftRuntime requires greedy Draft sampling")
        parallel = vllm_config.parallel_config
        if any(
            size != 1
            for size in (
                parallel.tensor_parallel_size,
                parallel.pipeline_parallel_size,
                parallel.data_parallel_size,
                parallel.decode_context_parallel_size,
                parallel.prefill_context_parallel_size,
            )
        ):
            raise ValueError("DraftRuntime requires a single GPU")
        if vllm_config.cache_config.enable_prefix_caching:
            raise ValueError("DraftRuntime does not use token-based prefix caching")

        self.max_num_reqs = vllm_config.scheduler_config.max_num_seqs
        self.max_num_tokens = vllm_config.scheduler_config.max_num_batched_tokens
        self.max_model_len = vllm_config.model_config.max_model_len
        self.num_draft_tokens = spec.num_speculative_tokens
        self.free_slots = list(reversed(range(self.max_num_reqs)))
        torch.cuda.set_device(self.device)
        init_method = get_distributed_init_method("127.0.0.1", get_open_port())
        init_worker_distributed_environment(
            vllm_config,
            rank=0,
            distributed_init_method=init_method,
            local_rank=self.device.index or 0,
            backend=current_platform.dist_backend,
        )
        self.distributed_initialized = True
        init_workspace_manager(self.device)

    @torch.inference_mode()
    def bootstrap(
        self, weights: dict[str, torch.Tensor], aux_hidden_state_count: int
    ) -> None:
        """Consume Target tensors, load EAGLE, allocate KV and capture native graphs.

        The server transfers ownership of ``weights`` here. Clear the received
        tensor dictionary after copying so its GPU storage is gone before profiling.
        """
        if self.speculator is not None:
            raise ValueError("DraftRuntime is already initialized")
        self.aux_hidden_state_count = aux_hidden_state_count
        with set_current_vllm_config(self.config), torch.device(self.device):
            target = self._vocabulary_model(weights)
            weights.clear()
            self.speculator = _StagedEagleSpeculator(self.config, self.device)
            self.speculator.load_model(target)
            self.model_state = DefaultModelState(
                self.config,
                self.speculator.model,
                encoder_cache=None,
                device=self.device,
            )
            # Shared modules are now retained by EAGLE. Drop any Target head or
            # embedding that the checkpoint supplied independently.
            del target
            self.input_buffers = InputBuffers(
                self.max_num_reqs, self.max_num_tokens, self.device
            )
            # Draft's input batch uses the native one-logit-per-request layout.
            # These constant mappings can be shared by all active batch sizes.
            self.cu_num_logits = torch.arange(
                self.max_num_reqs + 1, dtype=torch.int32, device=self.device
            )
            self.cu_num_logits_np = np.arange(self.max_num_reqs + 1, dtype=np.int32)
            self.expanded_local_pos = torch.zeros(
                self.max_num_reqs, dtype=torch.int32, device=self.device
            )
            self.last_sampled = torch.zeros(
                (self.max_num_reqs, 1), dtype=torch.int64, device=self.device
            )
            self.next_prefill = torch.zeros(
                (1, self.max_num_reqs), dtype=torch.int32, device=self.device
            )
            self.temperature = torch.zeros(self.max_num_reqs, device=self.device)
            self.seeds = torch.zeros(
                self.max_num_reqs, dtype=torch.int64, device=self.device
            )
            current_platform.update_block_size_for_backend(self.config)
            specs = get_kv_cache_spec(self.config)
            budget = self.kv_cache_memory_bytes
            if budget is None:
                budget = self._available_kv_memory()
            self.kv_config = get_kv_cache_configs(self.config, [specs], [budget])[0]
            self.config.cache_config.num_gpu_blocks = self.kv_config.num_blocks
            self.attn_groups, attn_cg_support, kernel_block_sizes = init_attn_backend(
                self.kv_config, self.config, self.device
            )
            block_sizes = [
                group.kv_cache_spec.block_size
                for group in self.kv_config.kv_cache_groups
            ]
            self.block_tables = BlockTables(
                block_sizes=block_sizes,
                max_num_reqs=self.max_num_reqs,
                max_num_batched_tokens=self.max_num_tokens,
                max_num_blocks_per_group=[
                    get_block_table_width(cdiv(self.max_model_len, size), size)
                    for size in block_sizes
                ],
                device=self.device,
                kernel_block_sizes=kernel_block_sizes,
            )
            self.speculator.set_attn(
                self.model_state,
                self.kv_config,
                self.block_tables,
                self.input_buffers,
                self.attn_groups,
            )
            init_kv_cache(
                self.kv_caches,
                self.config.compilation_config.static_forward_context,
                self.kv_config,
                self.attn_groups,
                self.device,
                self.config.cache_config.cache_dtype,
                kernel_block_sizes,
                self.config,
            )
            scheduler_block_size, hash_block_size = resolve_kv_cache_block_sizes(
                self.kv_config, self.config
            )
            self.kv_manager = KVCacheManager(
                self.kv_config,
                self.max_model_len,
                scheduler_block_size=scheduler_block_size,
                hash_block_size=hash_block_size,
                max_in_flight_tokens=self.max_num_tokens,
                enable_caching=False,
                use_eagle=True,
                num_prefill_lookahead=1,
            )
            attn_cg_support = attn_cg_support.narrow(
                *self.model_state.get_additional_cg_support()
            )
            compilation = self.config.compilation_config
            mode = compilation.resolve_cudagraph_mode_and_sizes(
                attn_cg_support.min_cg_support,
                attn_cg_support.min_cg_attn_backend,
                self.num_draft_tokens + 1,
                use_v2_model_runner=True,
                tensor_parallel_size=1,
                kv_cache_config=self.kv_config,
                max_num_reqs=self.max_num_reqs,
            )
            self.speculator.init_cudagraph_manager(mode)
            logger = logging.getLogger(__name__)
            logger.info(
                "Draft CUDA graph mode=%s, capture sizes=%s",
                mode.name,
                compilation.cudagraph_capture_sizes,
            )
            prefill = self.speculator.prefill_cudagraph_manager
            decode = self.speculator.decode_cudagraph_manager
            assert prefill is not None and decode is not None
            if prefill.needs_capture() or decode.needs_capture():
                # Native capture owns warmup, stable input buffers and its graph
                # pool. As in MRv2, utilization headroom remains available after KV.
                gc.collect()
                torch.cuda.empty_cache()
                free_before = torch.cuda.mem_get_info(self.device)[0]
                started = time.perf_counter()
                self.speculator.capture()
                graph_bytes = free_before - torch.cuda.mem_get_info(self.device)[0]
                logger.info(
                    "Draft CUDA graph capture finished in %.1f s, took %.2f GiB",
                    time.perf_counter() - started,
                    graph_bytes / (1 << 30),
                )

    def _vocabulary_model(self, weights: dict[str, torch.Tensor]) -> nn.Module:
        """Build only the two modules native EAGLE may share with its Target."""
        vocab_size = self.config.model_config.get_vocab_size()
        embedding_weight = weights["embed_tokens"]
        head_weight = weights["lm_head"]
        target = nn.Module()
        target.model = nn.Module()
        target.model.embed_tokens = VocabParallelEmbedding(
            vocab_size,
            embedding_weight.shape[1],
            params_dtype=embedding_weight.dtype,
            prefix="foretoken_dt_target.embed_tokens",
        )
        target.lm_head = ParallelLMHead(
            vocab_size,
            head_weight.shape[1],
            params_dtype=head_weight.dtype,
            prefix="foretoken_dt_target.lm_head",
        )
        for module, weight in (
            (target.model.embed_tokens, embedding_weight),
            (target.lm_head, head_weight),
        ):
            if tuple(module.weight.shape) != tuple(weight.shape):
                raise ValueError("Target vocabulary padding does not match Draft")
            if weight.is_cuda:
                # Receive buffers may have been allocated on the transport stream.
                weight.record_stream(torch.cuda.current_stream(self.device))
            module.weight.copy_(weight)
        return target

    def _available_kv_memory(self) -> int:
        """Profile native EAGLE's non-attention work before choosing a KV budget."""
        assert self.speculator is not None
        torch.cuda.empty_cache()
        torch.cuda.reset_peak_memory_stats(self.device)
        before = torch.cuda.memory_allocated(self.device)
        count = self.max_num_tokens
        # Keep received features live while profiling, as in a real proposal.
        target_hidden_size = self.config.model_config.get_hidden_size()
        feature_parts = [
            torch.zeros(
                count,
                target_hidden_size,
                dtype=self.config.model_config.dtype,
                device=self.device,
            )
            for _ in range(self.aux_hidden_state_count or 1)
        ]
        # A graph-padded proposal retains its received features while creating
        # local padded copies. Conservatively profile that extra storage at the
        # configured capture upper bound alongside the maximum-token forward.
        compilation = self.config.compilation_config
        padded_features = []
        if compilation.cudagraph_mode != CUDAGraphMode.NONE:
            padded_features = [
                torch.empty_like(part[: compilation.max_cudagraph_capture_size])
                for part in feature_parts
            ]
        if self.aux_hidden_state_count:
            # Native EAGLE3 concatenates the received tensors before projecting;
            # its temporary concatenation and received tensors coexist on GPU.
            features = torch.cat(feature_parts, dim=-1)
            hidden = self.speculator.model.combine_hidden_states(features)
            del features
        else:
            hidden = feature_parts[0]
        with set_forward_context(None, self.config, num_tokens=count, slot_mapping={}):
            output, draft_hidden = self.speculator.model(
                input_ids=self.input_buffers.input_ids,
                positions=self.input_buffers.positions,
                hidden_states=hidden,
            )
            self.speculator.model.compute_logits(output[: self.max_num_reqs])
        torch.cuda.synchronize(self.device)
        peak = torch.cuda.max_memory_allocated(self.device) - before
        del output, draft_hidden, hidden, feature_parts, padded_features
        torch.cuda.empty_cache()
        free, total = torch.cuda.mem_get_info(self.device)
        used = total - free
        budget = (
            int(total * self.config.cache_config.gpu_memory_utilization) - used - peak
        )
        if budget <= 0:
            raise ValueError("Draft model and activation memory leave no room for KV")
        return budget

    @torch.inference_mode()
    def propose(
        self,
        metadata: dict[str, Any],
        tensors: dict[str, torch.Tensor],
        feedback_ready: torch.cuda.Event | None = None,
        hidden_ready: torch.cuda.Event | None = None,
    ) -> dict[str, torch.Tensor]:
        """Rebuild local attention metadata and return K GPU candidates in batch order."""
        if self.speculator is None or self.kv_manager is None:
            raise ValueError("DraftRuntime must be bootstrapped before proposals")
        with set_current_vllm_config(self.config):
            batch, cg_mode = self._prepare_batch(metadata, tensors)
            layout = self.decode_layout
            if layout is not None and layout.blocks is not None:
                blocks = layout.blocks
            else:
                blocks = self.block_tables.gather_block_tables(
                    batch.idx_mapping, batch.num_reqs_after_padding
                )
                if layout is not None:
                    layout.blocks = blocks
            # EAGLE's subsequent draft steps reuse the slot-mapping storage.
            # Always restore this turn's mappings from its actual GPU positions.
            slots = self.block_tables.compute_slot_mappings(
                batch.idx_mapping,
                batch.query_start_loc,
                batch.positions,
                num_tokens_padded=batch.num_tokens_after_padding,
            )
            attn_metadata = self.model_state.prepare_attn(
                batch,
                cg_mode,
                blocks,
                slots,
                self.attn_groups,
                self.kv_config,
            )
            slot_mappings = build_slot_mappings_by_layer(slots, self.kv_config)
            # Known inputs and attention are prepared while Target runs forward.
            # Feature projection is the first stage that needs its hidden states.
            if hidden_ready is not None:
                torch.cuda.current_stream(self.device).wait_event(hidden_ready)
            aux = [
                tensors[f"aux_hidden_states_{i}"].to(self.device)
                for i in range(metadata["aux_hidden_state_count"])
            ]
            hidden = aux[0] if aux else tensors["last_hidden_states"].to(self.device)
            # Wire tensors contain real tokens only. Native EAGLE copies these
            # features into stable graph buffers with the locally selected shape.
            padding = batch.num_tokens_after_padding - batch.num_tokens
            if padding:
                if aux:
                    aux = [torch.nn.functional.pad(x, (0, 0, 0, padding)) for x in aux]
                    hidden = aux[0]
                else:
                    hidden = torch.nn.functional.pad(hidden, (0, 0, 0, padding))
            last = tensors["last_sampled"].reshape(batch.num_reqs, 1)
            slot = int(batch.idx_mapping_np[0])
            rows = slice(slot, slot + 1) if batch.num_reqs == 1 else batch.idx_mapping
            if feedback_ready is None:
                self.last_sampled[rows] = last.to(self.device, dtype=torch.int64)
            else:
                # Keep counts and per-request tokens untouched until native
                # EAGLE's first feedback consumer waits for the receive.
                if (
                    tensors["num_sampled"].dtype != torch.int32
                    or tensors["num_rejected"].dtype != torch.int32
                ):
                    raise ValueError("Unexpected DT sampler feedback layout")
                self.speculator.pending_feedback = (
                    feedback_ready,
                    self.last_sampled,
                    rows,
                    last,
                )
            rejected = tensors["num_rejected"].to(self.device, dtype=torch.int32)
            sampled = tensors["num_sampled"].to(self.device, dtype=torch.int32)
            draft_tokens = self.speculator.propose(
                batch,
                attn_metadata,
                slot_mappings,
                hidden,
                aux or None,
                sampled,
                rejected,
                self.last_sampled,
                self.next_prefill,
                self.temperature,
                self.seeds,
            )
            if self.speculator.pending_feedback is not None:
                raise RuntimeError("Native EAGLE did not consume this turn's feedback")
            # Target preposts this fixed-layout receive. Reject a broken native
            # result before the service sends a header or mismatched GPU payload.
            if (
                draft_tokens.shape != (batch.num_reqs, self.num_draft_tokens)
                or draft_tokens.dtype != torch.int64
                or draft_tokens.device != self.device
            ):
                raise ValueError("Draft candidates violate the fixed NCCL reply layout")
            return {"draft_tokens": draft_tokens}

    def _prepare_batch(
        self, metadata: dict[str, Any], tensors: dict[str, torch.Tensor]
    ) -> tuple[InputBatch, CUDAGraphMode]:
        """Map requests to Draft slots and pad inputs for its native graph dispatch."""
        assert self.kv_manager is not None
        assert self.speculator is not None
        req_ids = metadata["req_ids"]
        num_reqs = len(req_ids)
        scheduled = np.asarray(
            metadata["num_scheduled_tokens"][:num_reqs], dtype=np.int32
        )
        computed = np.asarray(
            metadata["num_computed_tokens"][:num_reqs], dtype=np.int32
        )
        prefill_len = np.asarray(metadata["prefill_len"][:num_reqs], dtype=np.int32)
        prefill_computed = np.asarray(
            metadata["num_computed_prefill_tokens"][:num_reqs], dtype=np.int32
        )
        num_tokens = int(scheduled.sum())
        if num_reqs > self.max_num_reqs or num_tokens > self.max_num_tokens:
            raise ValueError("Target batch exceeds Draft capacity")
        is_prefilling = prefill_computed < prefill_len
        has_prefill = bool(is_prefilling.any())
        uniform_tokens = get_uniform_decode_token_count(
            num_reqs, num_tokens, int(scheduled.max()), has_prefill
        )
        manager = self.speculator.prefill_cudagraph_manager
        assert manager is not None
        descriptor = manager.dispatch(
            num_reqs, num_tokens, uniform_tokens, num_active_loras=0
        )
        num_reqs_padded = descriptor.num_reqs or num_reqs
        num_tokens_padded = descriptor.num_tokens
        indices = []
        blocks_changed = False
        for i, req_id in enumerate(req_ids):
            session = self.sessions.get(req_id)
            is_new = session is None
            if session is None:
                if computed[i] != 0:
                    raise ValueError("A new Draft request must start at position zero")
                if not self.free_slots:
                    raise ValueError("Draft request slots are exhausted")
                session = _Session(
                    self.free_slots.pop(), _KVRequest(req_id, int(prefill_len[i]))
                )
                self.sessions[req_id] = session
            # Synchronous Target scheduling owns the confirmed prefix length.
            # Its next batch reflects rejection, so allocation needs no Draft
            # GPU-to-CPU acceptance readback. Native EAGLE overwrites the suffix.
            session.request.num_computed_tokens = int(computed[i])
            # The allocator's uncached path only consumes _KVRequest's fields;
            # token history and prefix hashes are owned by Target and never used here.
            allocated = self.kv_manager.allocate_slots(
                cast(Request, session.request),
                num_new_tokens=int(scheduled[i]),
                num_lookahead_tokens=self.num_draft_tokens,
            )
            if allocated is None:
                raise ValueError(
                    "Draft KV capacity exhausted; reduce admitted requests"
                )
            # Reset reused slots once; existing requests only publish new blocks,
            # matching MRv2's native request-update path.
            new_block_ids = allocated.get_block_ids(allow_none=not is_new)
            if new_block_ids is not None:
                self.block_tables.append_block_ids(
                    session.slot, new_block_ids, overwrite=is_new
                )
                blocks_changed = True
            indices.append(session.slot)
        if blocks_changed:
            self.block_tables.apply_staged_writes()

        full_decode = (
            descriptor.cg_mode == CUDAGraphMode.FULL
            and not has_prefill
            and uniform_tokens == self.num_draft_tokens + 1
            and metadata["query_start_loc"][: num_reqs + 1]
            == list(range(0, num_tokens + 1, self.num_draft_tokens + 1))
        )
        owners = (
            tuple(self.sessions[req_id] for req_id in req_ids) if full_decode else ()
        )
        shape = (num_reqs, num_reqs_padded, num_tokens, num_tokens_padded)
        layout = self.decode_layout
        if layout is not None and (
            not full_decode
            or layout.shape != shape
            or any(
                owner is not cached for owner, cached in zip(owners, layout.sessions)
            )
        ):
            self.decode_layout = layout = None
        buffers = self.input_buffers
        if layout is None:
            idx_np = np.asarray(indices, dtype=np.int32)
            idx = async_copy_to_gpu(idx_np, device=self.device)
            query_np = np.full(num_reqs_padded + 1, num_tokens, dtype=np.int32)
            query_np[: num_reqs + 1] = metadata["query_start_loc"][: num_reqs + 1]
            buffers.query_start_loc.fill_(num_tokens)
            async_copy_to_gpu(
                query_np, out=buffers.query_start_loc[: num_reqs_padded + 1]
            )
            logits_indices = buffers.query_start_loc[1 : num_reqs + 1] - 1
            buffers.input_ids[num_tokens:num_tokens_padded].zero_()
            buffers.positions[num_tokens:num_tokens_padded].zero_()
            buffers.is_padding[num_tokens:num_tokens_padded].fill_(True)
            buffers.seq_lens.zero_()
            if full_decode:
                self.decode_layout = _DecodeLayout(
                    owners, shape, idx, idx_np, query_np, logits_indices
                )
        else:
            idx, idx_np = layout.idx, layout.idx_np
            query_np, logits_indices = layout.query_np, layout.logits_indices
            if blocks_changed:
                # Native attention and graph replay use the compact table's
                # stable address. Refresh its contents when allocation grows.
                layout.blocks = None
        buffers.input_ids[:num_tokens].copy_(tensors["input_ids"])
        buffers.positions[:num_tokens].copy_(tensors["positions"])
        buffers.is_padding[:num_tokens].copy_(tensors["is_padding"])
        buffers.seq_lens[:num_reqs].copy_(tensors["seq_lens"])
        seq_upper_bound = torch.zeros(num_reqs_padded, dtype=torch.int32)
        seq_upper_bound[:num_reqs] = torch.tensor(
            metadata["seq_lens_cpu_upper_bound"][:num_reqs], dtype=torch.int32
        )
        # A contiguous one-row write needs no GPU scatter/index kernel.
        rows = slice(indices[0], indices[0] + 1) if num_reqs == 1 else idx
        self.next_prefill[:, rows] = (
            tensors["next_prefill_tokens"]
            .reshape(1, num_reqs)
            .to(self.device, dtype=torch.int32)
        )
        self.temperature[rows] = (
            tensors["temperature"].reshape(num_reqs).to(self.device)
        )
        self.seeds[rows] = tensors["seeds"].reshape(num_reqs).to(self.device)
        # Reuse the native no-draft sampling layout for Draft's own batch.
        # Target's expanded verification logits belong to its rejection sampler;
        # EAGLE consumes the real query plus num_sampled/num_rejected separately.
        batch = InputBatch(
            req_ids=req_ids,
            num_reqs=num_reqs,
            num_reqs_after_padding=num_reqs_padded,
            idx_mapping=idx,
            idx_mapping_np=idx_np,
            expanded_idx_mapping=idx,
            expanded_local_pos=self.expanded_local_pos[:num_reqs],
            num_scheduled_tokens=scheduled,
            num_tokens=num_tokens,
            num_tokens_after_padding=num_tokens_padded,
            num_draft_tokens=0,
            num_draft_tokens_per_req=None,
            query_start_loc=buffers.query_start_loc[: num_reqs_padded + 1],
            query_start_loc_np=query_np,
            seq_lens=buffers.seq_lens[:num_reqs_padded],
            seq_lens_cpu_upper_bound=seq_upper_bound,
            dcp_local_seq_lens=None,
            num_computed_tokens_np=computed,
            prefill_len_np=prefill_len,
            num_computed_prefill_tokens_np=prefill_computed,
            is_prefilling_np=is_prefilling,
            has_prefill=has_prefill,
            max_seq_len_np=None,
            input_ids=buffers.input_ids[:num_tokens_padded],
            positions=buffers.positions[:num_tokens_padded],
            is_padding=buffers.is_padding[:num_tokens_padded],
            logits_indices=logits_indices,
            cu_num_logits=self.cu_num_logits[: num_reqs + 1],
            cu_num_logits_np=self.cu_num_logits_np[: num_reqs + 1],
            has_structured_output_reqs=False,
            prompt_lens=None,
        )
        return batch, descriptor.cg_mode

    def free(self, req_ids: list[str]) -> None:
        """Release ended or reset requests' KV blocks and stable Draft slots."""
        for req_id in req_ids:
            session = self.sessions.pop(req_id, None)
            if session is None:
                continue
            if self.decode_layout is not None and any(
                owner is session for owner in self.decode_layout.sessions
            ):
                self.decode_layout = None
            assert self.kv_manager is not None
            self.kv_manager.free(cast(Request, session.request))
            self.free_slots.append(session.slot)

    def close(self) -> None:
        """Release request allocations and GPU groups before the server exits."""
        if self.closed:
            return
        self.closed = True
        self.free(list(self.sessions))
        self.kv_caches.clear()
        self.speculator = None
        self.kv_manager = None
        self.config.compilation_config.static_forward_context.clear()
        if self.distributed_initialized:
            destroy_model_parallel()
            destroy_distributed_environment()
            self.distributed_initialized = False
