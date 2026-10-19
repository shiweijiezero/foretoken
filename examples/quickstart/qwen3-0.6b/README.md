<!-- SPDX-License-Identifier: Apache-2.0 -->
<!-- SPDX-FileCopyrightText: Copyright contributors to the Foretoken project -->

# Single-Model Quick Start

[English](README.md) | [中文](README_zh.md)

For two models with autoscaling, see [Multi-Model Quick Start](../multi-model/README.md).

For prequantized weights or online quantization, see [Deploy a quantized model](../../feature/quantized-model/README.md).

This example deploys one frontend and one `Qwen/Qwen3-0.6B` model replica. The workload requests one GPU, 8 CPU, and 52 GiB memory; allow additional capacity for the platform. Models and runtime caches share the repository-root `data/` directory with the multi-model example, as configured in `cache.yaml`.

Configure the model, replica count, resources, parallelism, and [inference parameters](../../../docs/features/inference-parameters.md) in [`model.yaml`](model.yaml) (`ModelService`), the runtime cache in [`cache.yaml`](cache.yaml) (`RuntimeCache`), and the frontend in [`frontend.yaml`](frontend.yaml) (`FrontendService`). Foretoken creates the required Kubernetes workloads automatically.

## Deploy

Install the [platform from source](../../../docs/deployment/source.md) and prepare the [model storage](../../../docs/features/model-storage.md). Run from the repository root:

```bash
foretoken deploy examples/quickstart/qwen3-0.6b --timeout 20m
```

Without `--name`, the instance uses the `FrontendService` name from [`frontend.yaml`](frontend.yaml), `metadata.name: qwen3-0-6b`. The command creates that instance in the current kubeconfig namespace and waits for its services to be ready. Add `--name candidate-a` to deploy an independent instance called `candidate-a` instead; use that name in subsequent commands.

## Send a request

Resolve the frontend URL:

```bash
FRONTEND_URL="$(foretoken endpoint qwen3-0-6b)"
```

Send an OpenAI-compatible request:

```bash
curl "$FRONTEND_URL/v1/chat/completions" \
  -H 'Content-Type: application/json' \
  -d '{
    "model": "Qwen/Qwen3-0.6B",
    "messages": [{"role": "user", "content": "Reply with: Foretoken is ready"}],
    "max_tokens": 32,
    "temperature": 0
  }'
printf '\n'
```

## Clean up

```bash
foretoken delete qwen3-0-6b
```
