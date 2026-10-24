<!-- SPDX-License-Identifier: Apache-2.0 -->
<!-- SPDX-FileCopyrightText: Copyright contributors to the Foretoken project -->

# Multi-Model Quick Start

[English](README.md) | [中文](README_zh.md)

This example serves two models through one frontend:

- `Qwen/Qwen3-0.6B` scales from one to three replicas from queue demand.
- `unsloth/Llama-3.2-1B-Instruct` runs as one fixed replica.

The initial deployment requests two GPUs, 12 CPU cores, and 100 GiB memory. At full scale, three Qwen replicas and one Llama replica request four GPUs, 20 CPU cores, and 196 GiB memory, including the frontend. Allow additional capacity for the platform. Models use the platform's default [data storage](../../../docs/features/model-storage.md), sharing downloads with the single-model example while keeping runtime caches private to each instance. For the smallest deployment, see [Single-Model Quick Start](../qwen3-0.6b/README.md).

## Deploy

Install the [Foretoken platform](../../../README.md#2-install-the-kubernetes-platform), then run from the repository root:

```bash
foretoken deploy examples/quickstart/multi-model --timeout 20m
export FRONTEND_URL="$(foretoken endpoint multi-model)"
export REQUEST_HOST="$(foretoken endpoint multi-model --host)"
```

Without `--name`, the instance uses the `FrontendService` name from [`frontend.yaml`](frontend.yaml), `metadata.name: multi-model`. Add `--name candidate-a` to deploy an independent instance called `candidate-a` instead; use that name in subsequent commands.

## Observe queue autoscaling

The Qwen service evaluates queue demand every five seconds. It starts with one replica, changes by at most one replica per evaluation, and delays scale down for five minutes. The settings used by this example are in the [Qwen model configuration](model-qwen3-0.6b.yaml).

Open a separate terminal to follow the instance's services:

```bash
foretoken status multi-model --watch
```

In the terminal used for deployment, send 32 requests with at most eight in flight:

```bash
seq 1 32 | xargs -P8 -I{} sh -c '
  curl --fail --silent --show-error \
    "$FRONTEND_URL/v1/chat/completions" \
    -H "Host: $REQUEST_HOST" \
    -H "Content-Type: application/json" \
    -d "{\"model\":\"Qwen/Qwen3-0.6B\",\"messages\":[{\"role\":\"user\",\"content\":\"Explain Kubernetes request routing in detail.\"}],\"max_tokens\":512}"
'
```

Queue pressure can add Qwen replicas while this workload runs. Whether it does depends on available GPU capacity and request duration; the status view follows replica readiness as capacity changes.

## Send requests

Request Qwen:

```bash
curl --fail-with-body "$FRONTEND_URL/v1/chat/completions" \
  -H "Host: $REQUEST_HOST" \
  -H 'Content-Type: application/json' \
  -d '{
    "model": "Qwen/Qwen3-0.6B",
    "messages": [{"role": "user", "content": "Hello from Qwen"}]
  }'
printf '\n'
```

Request Llama:

```bash
curl --fail-with-body "$FRONTEND_URL/v1/chat/completions" \
  -H "Host: $REQUEST_HOST" \
  -H 'Content-Type: application/json' \
  -d '{
    "model": "unsloth/Llama-3.2-1B-Instruct",
    "messages": [{"role": "user", "content": "Hello from Llama"}],
    "max_tokens": 32
  }'
printf '\n'
```

## Clean up

```bash
foretoken delete multi-model
```
