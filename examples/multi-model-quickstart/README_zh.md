<!-- SPDX-License-Identifier: Apache-2.0 -->
<!-- SPDX-FileCopyrightText: Copyright contributors to the Foretoken project -->

# 多模型 Quick Start

[English](README.md) | [中文](README_zh.md)

如需最小的单模型部署，请参阅 [单模型 Quick Start](../quickstart/README_zh.md)。

本示例通过同一个 frontend 提供两个模型：

- `Qwen/Qwen3-0.6B`：根据请求队列在 1–3 个副本之间自动扩缩；
- `unsloth/Llama-3.2-1B-Instruct`：固定 1 个副本。

每个副本使用 1 张 GPU，因此集群需要 2–4 张可调度 GPU。准备 4 张 GPU 可以覆盖完整扩缩容范围。

## Queue autoscaling

Qwen 服务每 5 秒评估一次队列：

- 出现排队请求时，每轮增加 1 个 ModelGroup，最多扩容到 3 个；
- 队列为空且没有正在执行的请求时，每轮减少 1 个 ModelGroup，最少保留 1 个。

## 部署

先安装 Foretoken 平台，再从仓库根目录执行：

```bash
kubectl apply --server-side -k examples/multi-model-quickstart

kubectl wait --for=condition=Ready \
  --namespace foretoken-multi-model-demo \
  --timeout=15m \
  frontendservice/multi-model-frontend \
  modelservice/multi-model-qwen3-0.6b \
  modelservice/multi-model-llama3.2-1b
```

观察 Qwen 副本变化：

```bash
kubectl get modelpool,modelgroup \
  --namespace foretoken-multi-model-demo \
  --watch
```

## 发送请求

```bash
export FRONTEND_HOST="$(kubectl get service multi-model-frontend \
  --namespace foretoken-multi-model-demo \
  -o jsonpath='{.status.loadBalancer.ingress[0].ip}{.status.loadBalancer.ingress[0].hostname}')"
export FRONTEND_URL="http://$FRONTEND_HOST:8080"
```

请求 Qwen：

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

请求 Llama：

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

## 清理

```bash
kubectl delete --wait=true --timeout=10m \
  -k examples/multi-model-quickstart
```
