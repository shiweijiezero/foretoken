<!-- SPDX-License-Identifier: Apache-2.0 -->
<!-- SPDX-FileCopyrightText: Copyright contributors to the Foretoken project -->

# Deploy a quantized model

English | [简体中文](README_zh.md)

Serve Qwen2.5-0.5B-Instruct using a prequantized checkpoint or quantize ordinary weights during loading. Each configuration has a complete `model.yaml`; namespace, frontend and cache resources are shared under `shared/`.

| Configuration | Weight loading | Hardware |
| --- | --- | --- |
| [`awq/`](awq/model.yaml) | Prequantized AWQ checkpoint, FP16 activations | NVIDIA A100 |
| [`bitsandbytes/`](bitsandbytes/model.yaml) | Ordinary checkpoint, 4-bit quantization during loading | NVIDIA A100 |
| [`torchao-metax/`](torchao-metax/model.yaml) | Ordinary checkpoint, INT8 weight storage with floating-point linear execution | MetaX C500 |

The MetaX configuration dequantizes each linear layer for execution; it reduces stored weight memory rather than providing INT8 GEMM acceleration. Loading-time quantization does not export a new checkpoint.

## Install and choose a configuration

Install the [platform from this source checkout](../../docs/custom-deployment.md) with `foretoken install -e .`. The model-server build prepares the quantization dependencies for the selected accelerator. MetaX setup follows the [MetaX deployment guide](../../docs/metax-deployment.md).

Run from the repository root and choose one pair:

```bash
# Prequantized AWQ
EXAMPLE=examples/quantized-model/awq
MODEL=Qwen/Qwen2.5-0.5B-Instruct-AWQ

# Or: BitsAndBytes 4-bit on A100
# EXAMPLE=examples/quantized-model/bitsandbytes
# MODEL=Qwen/Qwen2.5-0.5B-Instruct

# Or: TorchAO INT8 on C500
# EXAMPLE=examples/quantized-model/torchao-metax
# MODEL=Qwen/Qwen2.5-0.5B-Instruct
```

Each choice uses the context length from the model configuration and requests one GPU, 3 CPU and 9 GiB of host memory including the frontend, plus platform capacity. The configurations are alternatives for the same service, not concurrent deployments.

All choices use the repository-root `data/` directory. For remote clusters, set an absolute node-visible path in [`shared/cache.yaml`](shared/cache.yaml); see [model storage](../../docs/model-storage.md).

## Deploy and send a request

```bash
foretoken deploy "$EXAMPLE" --timeout 20m
FRONTEND_URL="$(foretoken endpoint "$EXAMPLE")"

curl --fail-with-body "$FRONTEND_URL/v1/chat/completions" \
  -H 'Content-Type: application/json' \
  --data-binary @- <<EOF
{
  "model": "$MODEL",
  "messages": [{"role": "user", "content": "Explain quantization in one sentence."}],
  "max_tokens": 64,
  "temperature": 0
}
EOF
```

For Gateway access, follow the hostname and Host-header example in the root Quick Start. Configure the model directly in the selected `model.yaml`; native options are described in [inference parameters](../../docs/inference-parameters.md).

## Clean up

```bash
foretoken delete "$EXAMPLE"
```

The namespace and serving resources are removed; downloaded files remain in the directory cache.
