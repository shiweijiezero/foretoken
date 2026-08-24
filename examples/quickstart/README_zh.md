<!-- SPDX-License-Identifier: Apache-2.0 -->
<!-- SPDX-FileCopyrightText: Copyright contributors to the Foretoken project -->

# 单模型 Quick Start

[English](README.md) | [中文](README_zh.md)

如需运行双模型并验证自动扩缩容，请参阅 [多模型 Quick Start](../multi-model-quickstart/README_zh.md)。

本示例部署一个 frontend 和一个 `Qwen/Qwen3-0.6B` 模型副本，使用 1 张 GPU。

## 部署

先安装 Foretoken 平台，再从仓库根目录执行：

```bash
kubectl apply --server-side -k examples/quickstart

kubectl wait --for=condition=Ready \
  --namespace foretoken-demo \
  --timeout=15m \
  frontendservice/quickstart-frontend \
  modelservice/quickstart-qwen3-0.6b
```

## 发送请求

读取 `LoadBalancer` Service 地址：

```bash
export FRONTEND_HOST="$(kubectl get service quickstart-frontend \
  --namespace foretoken-demo \
  -o jsonpath='{.status.loadBalancer.ingress[0].ip}{.status.loadBalancer.ingress[0].hostname}')"
export FRONTEND_URL="http://$FRONTEND_HOST:8080"
```

发送 OpenAI API 兼容格式的请求：

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

## 清理

```bash
kubectl delete --wait=true --timeout=10m \
  -k examples/quickstart
```
