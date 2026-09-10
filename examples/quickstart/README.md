<!-- SPDX-License-Identifier: Apache-2.0 -->
<!-- SPDX-FileCopyrightText: Copyright contributors to the Foretoken project -->

# Single-Model Quick Start

[English](README.md) | [中文](README_zh.md)

For two models with autoscaling, see [Multi-Model Quick Start](../multi-model-quickstart/README.md).

This example deploys one frontend and one `Qwen/Qwen3-0.6B` model replica. The workload requests one GPU, 8 CPU, and 52 GiB memory; allow additional capacity for the platform. It also creates an automatically expanding runtime cache PVC starting at 10 GiB through the namespace's default `StorageClass`.

Configure the model, replica count, resources, and parallelism in [`model.yaml`](model.yaml) (`ModelService`), the runtime cache in [`cache.yaml`](cache.yaml) (`RuntimeCache`), and the frontend in [`frontend.yaml`](frontend.yaml) (`FrontendService`). Foretoken creates the required Kubernetes workloads automatically.

## Deploy

Follow the [root Quick Start](../../README.md) to install the platform and obtain the repository, then run from the repository root:

```bash
foretoken deploy examples/quickstart --timeout 20m
```

The command reports each service state as it changes and exits when the current configuration is ready.

## Send a request

Resolve the frontend URL:

```bash
FRONTEND_URL="$(foretoken endpoint examples/quickstart)"
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
foretoken delete examples/quickstart
```
