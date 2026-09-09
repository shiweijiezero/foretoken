<!-- SPDX-License-Identifier: Apache-2.0 -->
<!-- SPDX-FileCopyrightText: Copyright contributors to the Foretoken project -->

# Single-Model Quick Start

[English](README.md) | [中文](README_zh.md)

For two models with autoscaling, see [Multi-Model Quick Start](../multi-model-quickstart/README.md).

This example deploys one frontend and one `Qwen/Qwen3-0.6B` model replica. The workload requests one GPU, 8 CPU, and 52 GiB memory; allow additional capacity for the platform. Its `RuntimeCache` uses `./data` for preloaded model files and runtime caches. The deploy command creates a static directory PV. k3d must bind the directory into its node; other clusters require an absolute node path, shared on every node for multi-node deployments. Remove `directory` and set `initialSize` to use a dynamic PVC. See the [cache guide](../../docs/development/runtime-cache.md) for permissions, preparation, and retention.

Configure the model, replica count, resources, and parallelism in [`model.yaml`](model.yaml) (`ModelService`), the runtime cache in [`cache.yaml`](cache.yaml) (`RuntimeCache`), and the frontend in [`frontend.yaml`](frontend.yaml) (`FrontendService`). Foretoken creates the required Kubernetes workloads automatically.

Directory mode requires the current-source CLI and matching images, not the published 0.0.2 package.

## Deploy

First complete the [root Quick Start](../../README.md) through platform installation. The directory must exist before deployment. If the cluster is k3d, bind it into the node when creating the cluster as described in the [k3d guide](../../docs/k3d-deployment.md):

```bash
mkdir -p examples/quickstart/data
foretoken deploy examples/quickstart
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
