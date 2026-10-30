# SPDX-License-Identifier: Apache-2.0
# SPDX-FileCopyrightText: Copyright contributors to the Foretoken project

"""Retain confirmed features and expose them to the native neural speculator."""

from types import MethodType

import torch


class DraftContext:
    """Keep confirmed host features available for native KV eviction and recomputation."""

    def __init__(self):
        self.tensor = None
        self.end = 0
        self.current = None
        self.current_start = 0
        self.copied: torch.cuda.Event | None = None

    def reserve_history(self, end, feature_shape, dtype):
        """Reserve owned host history without advancing the confirmed feature prefix."""
        if end == 0:
            return
        previous = self.tensor
        if previous is not None and (
            previous.shape[1:] != feature_shape or previous.dtype != dtype
        ):
            raise ValueError("Target feature layout changed within a request")
        if previous is None or previous.shape[0] < end:
            self.tensor = torch.empty(
                (1 << (end - 1).bit_length(), *feature_shape),
                dtype=dtype,
                device="cpu",
                pin_memory=True,
            )
            if previous is not None:
                if self.copied is not None:
                    self.copied.synchronize()
                self.tensor[: self.end].copy_(previous[: self.end])

    def append(self, start, rows, copy_stream=None):
        """Retain the current GPU interval and a host history for native KV replay."""
        if start != self.end:
            raise ValueError("Target features must extend the retained prefix")
        if not rows.shape[0]:
            return
        end = start + rows.shape[0]
        self.reserve_history(end, rows.shape[1:], rows.dtype)
        self.current_start = start
        if rows.is_cuda:
            self.current = rows.clone()
            copy_stream.wait_stream(torch.cuda.current_stream(rows.device))
            with torch.cuda.stream(copy_stream):
                self.tensor[start:end].copy_(self.current, non_blocking=True)
                self.current.record_stream(copy_stream)
                if self.copied is None:
                    self.copied = torch.cuda.Event(blocking=True)
                self.copied.record(copy_stream)
        else:
            self.tensor[start:end].copy_(rows)
        self.end = end

    def read(self, start, count):
        """Return the scheduled interval, including replay after native KV preemption."""
        if start < 0 or start + count > self.end:
            raise ValueError("Draft execution exceeds its confirmed feature prefix")
        if self.current is not None and start >= self.current_start:
            offset = start - self.current_start
            return self.current[offset : offset + count]
        # Older intervals are needed only when native KV is replayed. The hot
        # turn reads its received GPU slice while the host history is retained.
        if self.copied is not None:
            self.copied.synchronize()
        return self.tensor[start : start + count]


class SharedTargetLoader:
    """Materialize shared Target weights without allocating its attention and MLP backbone."""

    def __init__(self, loader, runner):
        self.loader = loader
        self.runner = runner

    def load_model(self, vllm_config, model_config):
        """Supply shared weights and fixed feature inputs without a second Target backbone."""
        from vllm.model_executor.layers import rotary_embedding
        from vllm.model_executor.layers.layernorm import RMSNorm
        from vllm.model_executor.model_loader.reload.meta import materialize_meta_tensor
        from vllm.model_executor.model_loader.utils import (
            initialize_model,
            process_weights_after_loading,
        )
        from vllm.utils.torch_utils import set_default_torch_dtype
        from vllm.v1.worker.gpu.spec_decode.eagle.eagle3_utils import (
            get_eagle3_aux_layers_from_config,
        )
        from vllm.v1.worker.gpu.spec_decode.eagle.utils import get_target_lm_head

        from .runner import replace_binding

        with set_default_torch_dtype(model_config.dtype):
            context = vllm_config.compilation_config.static_forward_context
            original = set(context)
            # Attention and RoPE constructed on meta belong to the discarded
            # Target backbone, not the native drafter's cache or KV allocation.
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
            if (
                hasattr(model, "get_mtp_target_hidden_states")
                or getattr(language_model.model, "topk_indices_buffer", None)
                is not None
            ):
                raise ValueError(
                    "DT requires an adapter for this Target's additional MTP state"
                )
            inner = language_model.model
            embedding = getattr(inner, "embed_tokens", None) or getattr(
                inner, "embedding", None
            )
            shared = [embedding, get_target_lm_head(model, language_model)]
            shared.extend(
                layer
                for layer in model.modules()
                if isinstance(layer, (RMSNorm, torch.nn.LayerNorm))
            )
            materialized = {}
            with torch.device(self.runner.device):
                for root in shared:
                    if root is None:
                        continue
                    for layer in root.modules():
                        tensors = list(layer.named_parameters(recurse=False)) + list(
                            layer.named_buffers(recurse=False)
                        )
                        for name, tensor in tensors:
                            if tensor.is_meta:
                                if tensor not in materialized:
                                    materialized[tensor] = materialize_meta_tensor(
                                        tensor
                                    )
                                setattr(layer, name, materialized[tensor])
            self.loader.load_weights(model, model_config)
            process_weights_after_loading(
                torch.nn.ModuleList(layer for layer in shared if layer is not None),
                model_config,
                self.runner.device,
            )

        runner = self.runner
        aux_layers = (
            (
                get_eagle3_aux_layers_from_config(vllm_config.speculative_config)
                or model.get_eagle3_default_aux_hidden_state_layers()
            )
            if runner.use_aux_hidden_state_outputs
            else []
        )
        runner.context_features = torch.zeros(
            runner.max_num_tokens,
            len(aux_layers) or 1,
            model_config.get_hidden_size(),
            dtype=model_config.dtype,
            device=runner.device,
        )

        def forward(
            model,
            input_ids,
            positions,
            intermediate_tensors=None,
            inputs_embeds=None,
            **kwargs,
        ):
            """Expose fixed graph inputs; request history stays outside capture and replay."""
            features = runner.context_features[: len(positions)]
            if runner.use_aux_hidden_state_outputs:
                return features[:, 0], list(features.unbind(dim=1))
            return features[:, 0]

        model.forward = MethodType(forward, model)
        return model.eval()
