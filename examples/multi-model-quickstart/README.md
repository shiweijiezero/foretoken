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

The Qwen service evaluates its queue every five seconds. It calculates desired capacity from one average waiting request per Group, adds at most one ModelGroup per evaluation, and scales from one to three Groups. Scale down begins only after the recommendation has remained lower for five minutes.

## Deploy

Install the Foretoken platform first, then install the CLI and deploy from the repository root:

```bash
pip install -e .
foretoken deploy examples/multi-model-quickstart
```

The command discovers every model in the rendered configuration, reports service state changes, and exits when the current configuration is ready.

Watch the Qwen replicas change:

```bash
kubectl get modelpool,modelgroup \
  --namespace foretoken-multi-model-demo \
  --watch
```

## Send requests

```bash
FRONTEND_URL="$(foretoken endpoint examples/multi-model-quickstart)"
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
foretoken delete examples/multi-model-quickstart
```
