# SPDX-License-Identifier: Apache-2.0
# SPDX-FileCopyrightText: Copyright contributors to the Foretoken project

"""Use native TorchAO INT8 weight storage with MACA floating-point linear kernels."""

import torch
import torch.nn.functional as F
from torchao.quantization import Int8WeightOnlyConfig
from vllm.model_executor.layers.linear import LinearMethodBase
from vllm.model_executor.layers.quantization import register_quantization_config
from vllm.model_executor.layers.quantization.torchao import (
    TorchAOConfig,
    TorchAOLinearMethod,
)
from vllm.platforms import current_platform


class MacaTorchAOLinearMethod(TorchAOLinearMethod):
    """Keep quantized weights and materialize only the current linear operation."""

    def apply(
        self, layer: torch.nn.Module, x: torch.Tensor, bias: torch.Tensor | None = None
    ) -> torch.Tensor:
        """Execute vLLM's linear layer without MACA's incorrect INT8 tensor dispatch."""
        return F.linear(x, layer.weight.dequantize(), bias)


class MacaTorchAOConfig(TorchAOConfig):
    """Retain vLLM's loading and module selection for supported INT8 configurations."""

    def get_quant_method(
        self, layer: torch.nn.Module, prefix: str
    ) -> LinearMethodBase | None:
        """Select the MACA linear method while preserving upstream skipped modules."""
        method = super().get_quant_method(layer, prefix)
        if not isinstance(method, TorchAOLinearMethod):
            return method
        if not isinstance(method.quant_config.torchao_config, Int8WeightOnlyConfig):
            raise ValueError("MetaX TorchAO supports Int8WeightOnlyConfig")
        return MacaTorchAOLinearMethod(method.quant_config)


def register() -> None:
    """Register the MetaX execution adapter when vLLM loads its general plugins."""
    if current_platform.device_name != "maca":
        return
    register_quantization_config("torchao")(MacaTorchAOConfig)
    if "torchao" not in current_platform.supported_quantization:
        current_platform.supported_quantization.append("torchao")
