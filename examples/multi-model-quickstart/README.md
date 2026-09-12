<!-- SPDX-License-Identifier: Apache-2.0 -->
<!-- SPDX-FileCopyrightText: Copyright contributors to the Foretoken project -->

# Multi-Model Quick Start

[English](README.md) | [中文](README_zh.md)

This example serves two models through one frontend:

- `Qwen/Qwen3-0.6B` scales from one to three replicas from queue demand.
- `unsloth/Llama-3.2-1B-Instruct` runs as one fixed replica.

The initial deployment requests two GPUs, 12 CPU cores, and 100 GiB memory. At full scale, three Qwen replicas and one Llama replica request four GPUs, 20 CPU cores, and 196 GiB memory, including the frontend. Allow additional capacity for the platform. Models and runtime caches share the `./data` directory configured in `cache.yaml`. For the smallest deployment, see [Single-Model Quick Start](../quickstart/README.md).

## Deploy

Install the [platform from source](../../docs/custom-deployment.md) and prepare the [model storage](../../docs/model-storage.md). Run from the repository root:

```bash
foretoken deploy examples/multi-model-quickstart --timeout 20m
export FRONTEND_URL="$(foretoken endpoint examples/multi-model-quickstart)"
```

## Observe queue autoscaling

The Qwen service evaluates queue demand every five seconds. It starts with one replica, changes by at most one replica per evaluation, and delays scale down for five minutes. See the [autoscaling guide](../../docs/autoscaling.md) for the configuration and status contract.

Open a separate terminal to watch the Qwen capacity resources:

```bash
kubectl get modelpool,modelgroup \
  --namespace foretoken-multi-model-demo \
  --watch
```

In the terminal used for deployment, send 32 requests with at most eight in flight:

```bash
seq 1 32 | xargs -P8 -I{} sh -c '
  curl --fail --silent --show-error \
    "$FRONTEND_URL/v1/chat/completions" \
    -H "Content-Type: application/json" \
    -d "{\"model\":\"Qwen/Qwen3-0.6B\",\"messages\":[{\"role\":\"user\",\"content\":\"Explain Kubernetes request routing in detail.\"}],\"max_tokens\":512}"
'
```

Queue pressure can add Qwen replicas while this workload runs. Whether it does depends on the available GPU capacity and request duration. Inspect the applied capacity and decision reasons with:

```bash
kubectl get modelservice multi-model-qwen3-0.6b \
  --namespace foretoken-multi-model-demo \
  -o json | jq '.status.autoscaling[] | {
    id,
    kind,
    role,
    observationState,
    direction,
    desiredReplicas: .decision.desiredReplicas,
    adjustedReplicas: .adjustment.adjustedReplicas,
    appliedReplicas
  }'
```

## Send requests

Request Qwen:

```bash
curl --fail-with-body "$FRONTEND_URL/v1/chat/completions" \
  -H 'Content-Type: application/json' \
  -d '{
    "model": "Qwen/Qwen3-0.6B",
    "messages": [{"role": "user", "content": "Hello from Qwen"}],
    "max_tokens": 32
  }'
printf '\n'
```

Request Llama:

```bash
curl --fail-with-body "$FRONTEND_URL/v1/chat/completions" \
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
foretoken delete examples/multi-model-quickstart
```
