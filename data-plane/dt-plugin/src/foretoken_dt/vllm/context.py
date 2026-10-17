# SPDX-License-Identifier: Apache-2.0
# SPDX-FileCopyrightText: Copyright contributors to the Foretoken project

"""Target feature retention and native neural Draft execution inputs."""

from __future__ import annotations

from types import MethodType

import torch
from vllm.v1.worker.gpu.spec_decode.speculator import BaseSpeculator


class TargetContext(BaseSpeculator):
    """Retain computed features without loading or executing a Target-local drafter."""

    supports_mm_inputs = False
    draft_logits = None

    def __init__(self, config, device):
        self.budget = config.speculative_config.num_speculative_tokens
        self.device = device
        self.requests: dict[str, list[tuple[int, torch.Tensor]]] = {}

    def init_cudagraph_manager(self, mode):
        """The external role executes eagerly and owns no draft CUDA graphs."""

    def capture(self):
        """External proposal execution has no local graph to capture."""

    def propose(
        self,
        input_batch,
        attn_metadata,
        slot_mappings,
        last_hidden_states,
        aux_hidden_states,
        num_sampled,
        num_rejected,
        last_sampled,
        next_prefill_tokens,
        temperature,
        seeds,
        dp_sync=None,
        dummy_run=False,
        skip_attn_for_dummy_run=False,
        mm_inputs=None,
        is_profile=False,
    ):
        """Retain position-indexed features; the scheduler later selects confirmed rows."""
        if not dummy_run:
            features = [last_hidden_states, *(aux_hidden_states or [])]
            for index, req_id in enumerate(input_batch.req_ids):
                start = int(input_batch.num_computed_tokens_np[index])
                left, right = input_batch.query_start_loc_np[index : index + 2]
                chunks = self.requests.setdefault(req_id, [])
                # A rejection or preemption replaces the speculative suffix. Do not
                # accumulate stale verification frames across the request lifetime.
                while chunks and chunks[-1][0] >= start:
                    chunks.pop()
                if chunks:
                    position, previous = chunks[-1]
                    if previous.shape[0] > start - position:
                        chunks[-1] = (position, previous[: start - position].clone())
                chunks.append(
                    (start, torch.stack([f[left:right] for f in features], dim=1))
                )
        # Native MRV2 expects a draft-token tensor. ExternalDraftTokens suppresses
        # publication, and the next admission overwrites these unused placeholders.
        return torch.zeros(
            input_batch.num_reqs, self.budget, dtype=torch.int64, device=self.device
        )

    def snapshot(self, request_id, computed_tokens):
        """Copy a paused request's confirmed prefix for one independently owned export."""
        chunks = self.requests[request_id]
        end = 0
        pieces = []
        for index, (start, tensor) in enumerate(chunks):
            if start != end:
                raise RuntimeError("Target context contains a gap")
            count = min(tensor.shape[0], computed_tokens - start)
            if count <= 0:
                break
            if count < tensor.shape[0]:
                tensor = tensor[:count].clone()
                chunks[index] = (start, tensor)
            pieces.append(tensor[:count])
            end += count
        if end != computed_tokens:
            raise RuntimeError("Target context does not cover the confirmed prefix")
        return torch.cat(pieces, dim=0)


class SharedTargetLoader:
    """Load shared embeddings, output heads and norms without Target attention/MLP weights."""

    def __init__(self, loader, runner):
        self.loader = loader
        self.runner = runner

    def load_model(self, vllm_config, model_config):
        """Provide native speculator weight sharing and a feature-only Target forward."""
        from vllm.model_executor.layers import rotary_embedding
        from vllm.model_executor.layers.layernorm import RMSNorm
        from vllm.model_executor.model_loader.reload.meta import materialize_meta_tensor
        from vllm.model_executor.model_loader.utils import initialize_model
        from vllm.utils.torch_utils import set_default_torch_dtype
        from vllm.v1.worker.gpu.spec_decode.eagle.eagle3_utils import (
            get_eagle3_aux_layers_from_config,
        )
        from vllm.v1.worker.gpu.spec_decode.eagle.utils import get_target_lm_head

        from .runner import replace_binding

        with set_default_torch_dtype(model_config.dtype):
            context = vllm_config.compilation_config.static_forward_context
            original = set(context)
            # Meta RoPE buffers must not escape into the native drafter's cache.
            with (
                replace_binding(rotary_embedding, "_ROPE_DICT", {}),
                torch.device("meta"),
            ):
                model = initialize_model(
                    vllm_config=vllm_config, model_config=model_config
                )
            for name in set(context) - original:
                del context[name]
            language_model = (
                model.get_language_model()
                if hasattr(model, "get_language_model")
                else model
            )
            if getattr(language_model.model, "topk_indices_buffer", None) is not None:
                raise ValueError(
                    "neural DT does not yet support Target-shared sparse attention indices"
                )
            target_inner = language_model.model
            embedding = getattr(target_inner, "embed_tokens", None) or getattr(
                target_inner, "embedding", None
            )
            output_modules = [embedding, get_target_lm_head(model, language_model)]
            # compute_logits may apply normalization outside the shared LM head.
            # Select native normalization modules without assuming their position or name.
            output_modules.extend(
                layer
                for layer in model.modules()
                if isinstance(layer, (RMSNorm, torch.nn.LayerNorm))
            )
            materialized = {}
            with torch.device(self.runner.device):
                for root in output_modules:
                    if root is None:
                        continue
                    for layer in root.modules():
                        tensors = list(layer.named_parameters(recurse=False)) + list(
                            layer.named_buffers(recurse=False)
                        )
                        for name, tensor in tensors:
                            if tensor.is_meta:
                                # Preserve tied embedding/head weights across materialization.
                                if tensor not in materialized:
                                    materialized[tensor] = materialize_meta_tensor(
                                        tensor
                                    )
                                setattr(layer, name, materialized[tensor])
            model.load_weights(self.loader.get_all_weights(model_config, model))

        runner = self.runner

        def forward(
            model,
            input_ids,
            positions,
            intermediate_tensors=None,
            inputs_embeds=None,
            **kwargs,
        ):
            """Consume staged features, or synthetic inputs during profiling and warmup."""
            if runner.context_batch is None or runner.warming_up:
                hidden = torch.zeros(
                    len(positions),
                    model_config.get_hidden_size(),
                    dtype=model_config.dtype,
                    device=positions.device,
                )
                if runner.use_aux_hidden_state_outputs:
                    layers = (
                        get_eagle3_aux_layers_from_config(
                            vllm_config.speculative_config
                        )
                        or model.get_eagle3_default_aux_hidden_state_layers()
                    )
                    return hidden, [hidden] * len(layers)
                return hidden
            batch = runner.context_batch
            rows = []
            for index, req_id in enumerate(batch.req_ids):
                artifact_id = runner.context_requests[req_id][0]
                artifact = runner.external_speculation_io.artifacts[artifact_id]
                start = int(batch.num_computed_tokens_np[index])
                count = int(batch.num_scheduled_tokens[index])
                rows.append(artifact.buffer.tensor[start : start + count])
            features = torch.cat(rows, dim=0)
            for req_id in batch.req_ids:
                artifact = runner.external_speculation_io.artifacts[
                    runner.context_requests[req_id][0]
                ]
                artifact.event = torch.cuda.Event()
                artifact.event.record(torch.cuda.current_stream(runner.device))
            hidden = features[:, 0]
            if runner.use_aux_hidden_state_outputs:
                return hidden, list(features[:, 1:].unbind(dim=1))
            return hidden

        model.forward = MethodType(forward, model)
        return model.eval()
