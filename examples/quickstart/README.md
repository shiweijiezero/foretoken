<!-- SPDX-License-Identifier: Apache-2.0 -->
<!-- SPDX-FileCopyrightText: Copyright contributors to the Foretoken project -->

# Single-Model Quick Start

[English](README.md) | [中文](README_zh.md)

For two models with autoscaling, see [Multi-Model Quick Start](../multi-model-quickstart/README.md).

This example deploys one frontend and one `Qwen/Qwen3-0.6B` model replica using one GPU.

## Deploy

Install the Foretoken platform first, then run from the repository root:

```bash
kubectl apply --server-side -k examples/quickstart

kubectl wait --for=condition=Ready \
  --namespace foretoken-demo \
  --timeout=6m \
  frontendservice/quickstart-frontend \
  modelservice/quickstart-qwen3-0.6b
```

## Send a request

Read the `LoadBalancer` Service address:

```bash
export FRONTEND_HOST="$(kubectl get service quickstart-frontend \
  --namespace foretoken-demo \
  -o jsonpath='{.status.loadBalancer.ingress[0].ip}{.status.loadBalancer.ingress[0].hostname}')"
export FRONTEND_URL="http://$FRONTEND_HOST:8080"
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
kubectl delete --wait=true --timeout=10m \
  -k examples/quickstart
```
