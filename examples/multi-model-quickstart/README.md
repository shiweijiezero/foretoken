<!-- SPDX-License-Identifier: Apache-2.0 -->
<!-- SPDX-FileCopyrightText: Copyright contributors to the Foretoken project -->

# Multi-Model Quick Start

[English](README.md) | [中文](README_zh.md)

This example serves two models through one frontend:

- `Qwen/Qwen3-0.6B` scales from one to three replicas from queue demand.
- `unsloth/Llama-3.2-1B-Instruct` runs as one fixed replica.

The initial workload requests two GPUs, 12 CPU, and 100 GiB memory, including both frontend replicas. At full scale it requests four GPUs, 20 CPU, and 196 GiB memory; allow additional capacity for the platform. For the smallest deployment, see [Single-Model Quick Start](../quickstart/README.md).

The example creates an automatically expanding `ReadWriteMany` runtime cache PVC starting at 10 GiB. The cluster's default `StorageClass` must support `ReadWriteMany` and volume expansion. The status commands below also require `jq`.

Each model has its own `ModelService` manifest: [`model-qwen3-0.6b.yaml`](model-qwen3-0.6b.yaml) and [`model-llama3.2-1b.yaml`](model-llama3.2-1b.yaml). Both use the `RuntimeCache` in [`cache.yaml`](cache.yaml) and are served through the `FrontendService` in [`frontend.yaml`](frontend.yaml). Foretoken creates the required Kubernetes workloads automatically.

## Deploy

Complete the platform installation in the [root Quick Start](../../README.md), then run:

```bash
foretoken deploy examples/multi-model-quickstart
```

## Send requests

Resolve the frontend URL in the terminal you will use for requests and the concurrent workload:

```bash
export FRONTEND_URL="$(foretoken endpoint examples/multi-model-quickstart)"
```

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

## Observe queue autoscaling

The Qwen service evaluates queue demand every five seconds. It starts with one replica, changes by at most one replica per evaluation, and delays scale down for five minutes. See the [autoscaling guide](../../docs/autoscaling.md) for the configuration and status contract.

In a separate terminal, watch the Qwen service's applied and ready replica counts:

```bash
kubectl get modelservice multi-model-qwen3-0.6b \
  --namespace foretoken-multi-model-demo \
  -o 'custom-columns=NAME:.metadata.name,APPLIED:.status.autoscaling[*].appliedReplicas,READY:.status.autoscaling[*].readyReplicas' \
  --watch
```

Back in the request terminal, send 32 requests with at most eight in flight:

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
    observationState,
    direction,
    desiredReplicas: .decision.desiredReplicas,
    adjustedReplicas: .adjustment.adjustedReplicas,
    appliedReplicas,
    decisionReason: .decision.reason,
    adjustmentReason: .adjustment.reason,
    constraint: .constraint.reason
  }'
```

## Clean up

```bash
foretoken delete examples/multi-model-quickstart
```
