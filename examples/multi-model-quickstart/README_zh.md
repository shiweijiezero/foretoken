<!-- SPDX-License-Identifier: Apache-2.0 -->
<!-- SPDX-FileCopyrightText: Copyright contributors to the Foretoken project -->

# 多模型快速开始

[English](README.md) | [中文](README_zh.md)

本示例通过同一个前端服务提供两个模型：

- `Qwen/Qwen3-0.6B` 根据请求队列从 1 个副本扩缩到 3 个副本。
- `unsloth/Llama-3.2-1B-Instruct` 固定运行 1 个副本。

每个副本使用 1 张 GPU。完整扩缩范围最多需要 4 张可调度 GPU：Qwen 最多 3 张，Llama 1 张。示例中的 `ReadWriteMany` `RuntimeCache` 使用 `./data` 保存预加载模型文件和运行时缓存，并创建静态目录 PV。普通 Kubernetes 需填写绝对路径，多节点时该目录必须已经通过共享文件系统挂载到所有节点；删除 `directory` 并设置 `initialSize` 可改用动态 PVC，具体权限、目录准备和保留规则见[缓存指南](../../docs/development/runtime-cache_zh.md)。如需最小部署，请参阅[单模型快速开始](../quickstart/README_zh.md)。

目录模式需要当前源码 CLI 和匹配镜像，不适用于已发布的 0.0.2 包。

## 部署

先完成[根目录快速开始](../../README_zh.md)中的平台安装。使用 k3d 时，请按 [k3d 指南](../../docs/k3d-deployment_zh.md) 在创建集群时将该目录绑定到节点：

```bash
mkdir -p examples/multi-model-quickstart/data
foretoken deploy examples/multi-model-quickstart
export FRONTEND_URL="$(foretoken endpoint examples/multi-model-quickstart)"
```

## 观察队列自动扩缩容

Qwen 服务每 5 秒评估一次队列负载，从 1 个副本开始，每次评估最多调整 1 个副本，缩容前等待 5 分钟。配置和状态说明见[自动扩缩容指南](../../docs/autoscaling_zh.md)。

在一个终端中观察 Qwen 容量资源：

```bash
kubectl get modelpool,modelgroup \
  --namespace foretoken-multi-model-demo \
  --watch
```

在另一个终端运行有界并发负载。该命令发送 32 个请求，同时最多运行 8 个：

```bash
export FRONTEND_URL="$(foretoken endpoint examples/multi-model-quickstart)"

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

## 发送请求

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

## 清理

```bash
foretoken delete examples/multi-model-quickstart
```
