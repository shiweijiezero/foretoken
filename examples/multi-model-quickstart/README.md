<!-- SPDX-License-Identifier: Apache-2.0 -->
<!-- SPDX-FileCopyrightText: Copyright contributors to the Foretoken project -->

# Multi-Model Quick Start

[English](README.md) | [中文](README_zh.md)

For the minimal single-model deployment, see [Single-Model Quick Start](../quickstart/README.md).

This example serves two models through one frontend:

- `Qwen/Qwen3-0.6B`: queue-based autoscaling from one to three replicas;
- `unsloth/Llama-3.2-1B-Instruct`: one fixed replica.

Each replica uses one GPU, so the cluster needs two to four schedulable GPUs. Four GPUs cover the full autoscaling range.

## Queue autoscaling

The Qwen service evaluates its queue every five seconds:

- queued requests add one ModelGroup per evaluation, up to three;
- an empty queue with no active requests removes one ModelGroup per evaluation, down to one.

## Deploy

Install the Foretoken platform first, then run from the repository root:

```bash
kubectl apply --server-side -k examples/multi-model-quickstart

kubectl wait --for=condition=Ready \
  --namespace foretoken-multi-model-demo \
  --timeout=15m \
  frontendservice/multi-model-frontend \
  modelservice/multi-model-qwen3-0.6b \
  modelservice/multi-model-llama3.2-1b
```

Watch the Qwen replicas change:

```bash
kubectl get modelpool,modelgroup \
  --namespace foretoken-multi-model-demo \
  --watch
```

## Send requests

```bash
export FRONTEND_HOST="$(kubectl get service multi-model-frontend \
  --namespace foretoken-multi-model-demo \
  -o jsonpath='{.status.loadBalancer.ingress[0].ip}{.status.loadBalancer.ingress[0].hostname}')"
export FRONTEND_URL="http://$FRONTEND_HOST:8080"
```

Request Qwen:

```bash
curl "$FRONTEND_URL/v1/chat/completions" \
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
curl "$FRONTEND_URL/v1/chat/completions" \
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
kubectl delete --wait=true --timeout=10m \
  -k examples/multi-model-quickstart
```
