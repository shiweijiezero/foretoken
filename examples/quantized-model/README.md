<!-- SPDX-License-Identifier: Apache-2.0 -->
<!-- SPDX-FileCopyrightText: Copyright contributors to the Foretoken project -->

# Deploy a quantized model

English | [简体中文](README_zh.md)

These examples serve Qwen2.5-0.5B-Instruct with a prequantized checkpoint or quantize ordinary weights while loading. Each directory is a complete Kustomize deployment with its own namespace, runtime cache, frontend, and model service. The deployments can be created and removed independently without resource-name conflicts. A matching BF16 reference is included for distribution comparisons.

Build and install the [platform from this source checkout](../../docs/custom-deployment.md) before deploying an example:

```bash
foretoken install -e .
```

Each deployment requests one GPU, 3 CPU, and 9 GiB of host memory across the model and frontend, plus capacity for the Foretoken platform.

## AWQ on NVIDIA GPUs

This example loads the prequantized `Qwen/Qwen2.5-0.5B-Instruct-AWQ` checkpoint with FP16 activations. It requires one NVIDIA GPU with the selected vLLM quantization support.

```bash
foretoken deploy examples/quantized-model/awq --timeout 20m
```

When finished, remove only the AWQ deployment:

```bash
foretoken delete examples/quantized-model/awq
```

## BitsAndBytes 4-bit on NVIDIA GPUs

This example loads `Qwen/Qwen2.5-0.5B-Instruct` and applies 4-bit BitsAndBytes quantization during loading. It requires one NVIDIA GPU with the selected vLLM quantization support. Loading-time quantization does not create a new checkpoint.

```bash
foretoken deploy examples/quantized-model/bitsandbytes --timeout 20m
```

When finished, remove only the BitsAndBytes deployment:

```bash
foretoken delete examples/quantized-model/bitsandbytes
```

## Compare with BF16

From the repository root, compare bitsandbytes against the same unquantized model and BF16 computation:

```bash
foretoken eval examples/quantized-model/bitsandbytes \
  --reference examples/quantized-model/bf16 --output local
```

To include the BF16 self-comparison in bit-width plots, replace the candidate directory with `--candidates examples/quantized-model/candidates.jsonl`. The AWQ example uses FP16 activations, so its comparison against BF16 includes that computation-precision difference.

See [model distribution comparison](../../benchmarks/docs/eval/fidelity.md) for metrics, custom candidates, resuming a run, and updating existing deployments.

## Storage and requests

Each example's `cache.yaml` uses the repository-root `data/` directory. For a remote cluster, replace `directory: ../../../data` in the example you deploy with an absolute path visible to the target nodes; see [model storage](../../docs/model-storage.md).

Resolve the selected frontend with `foretoken endpoint` and use the model identifier from its section in an OpenAI-compatible request. The request format and Gateway hostname setup are shown in the [Single-Model Quick Start](../quickstart/README.md). Downloaded model files remain in the directory cache after cleanup.
