<!-- SPDX-License-Identifier: Apache-2.0 -->
<!-- SPDX-FileCopyrightText: Copyright contributors to the Foretoken project -->

# 多模型快速开始

[English](README.md) | [中文](README_zh.md)

本示例通过同一个前端服务提供两个模型：

- `Qwen/Qwen3-0.6B` 根据请求队列从 1 个副本扩缩到 3 个副本。
- `unsloth/Llama-3.2-1B-Instruct` 固定运行 1 个副本。

初始工作负载请求 2 张 GPU、12 个 CPU 和 100 GiB 内存，已计入两个前端副本。扩容到上限时请求 4 张 GPU、20 个 CPU 和 196 GiB 内存；还需为平台预留额外容量。如需最小部署，请参阅[单模型快速开始](../quickstart/README_zh.md)。

示例会创建一个从 10 GiB 起自动扩容的 `ReadWriteMany` 运行时缓存 PVC，集群的默认 `StorageClass` 必须支持 `ReadWriteMany` 和卷扩容。下文的状态查询命令还需要 `jq`。

两个模型分别通过 [`model-qwen3-0.6b.yaml`](model-qwen3-0.6b.yaml) 和 [`model-llama3.2-1b.yaml`](model-llama3.2-1b.yaml) 中的 `ModelService` 配置，共享 [`cache.yaml`](cache.yaml) 中的 `RuntimeCache`，并由 [`frontend.yaml`](frontend.yaml) 中的 `FrontendService` 提供访问入口。Foretoken 会自动创建所需的 Kubernetes 工作负载。

## 部署

先完成[根目录快速开始](../../README_zh.md)中的平台安装，再运行：

```bash
foretoken deploy examples/multi-model-quickstart
```

## 发送请求

在用于发送请求和运行并发负载的终端中获取前端 URL：

```bash
export FRONTEND_URL="$(foretoken endpoint examples/multi-model-quickstart)"
```

请求 Qwen：

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

请求 Llama：

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

## 观察队列自动扩缩容

Qwen 服务每 5 秒评估一次队列负载，从 1 个副本开始，每次评估最多调整 1 个副本，缩容前等待 5 分钟。配置和状态说明见[自动扩缩容指南](../../docs/autoscaling_zh.md)。

另开一个终端，观察 Qwen 服务已应用的副本数和就绪副本数：

```bash
kubectl get modelservice multi-model-qwen3-0.6b \
  --namespace foretoken-multi-model-demo \
  -o 'custom-columns=NAME:.metadata.name,APPLIED:.status.autoscaling[*].appliedReplicas,READY:.status.autoscaling[*].readyReplicas' \
  --watch
```

回到发送请求的终端，运行以下负载，共发送 32 个请求，同时最多运行 8 个：

```bash
seq 1 32 | xargs -P8 -I{} sh -c '
  curl --fail --silent --show-error \
    "$FRONTEND_URL/v1/chat/completions" \
    -H "Content-Type: application/json" \
    -d "{\"model\":\"Qwen/Qwen3-0.6B\",\"messages\":[{\"role\":\"user\",\"content\":\"详细解释 Kubernetes 请求路由。\"}],\"max_tokens\":512}"
'
```

负载运行期间，队列压力可能增加 Qwen 副本。是否扩容取决于可用 GPU 容量和请求持续时间。使用以下命令查看实际应用的容量和决策原因：

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

## 清理

```bash
foretoken delete examples/multi-model-quickstart
```
