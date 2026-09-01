<!-- SPDX-License-Identifier: Apache-2.0 -->
<!-- SPDX-FileCopyrightText: Copyright contributors to the Foretoken project -->

# 多模型快速开始

[English](README.md) | [中文](README_zh.md)

如需最小的单模型部署，请参阅[单模型快速开始](../quickstart/README_zh.md)。

本示例通过同一个前端服务提供两个模型：

- `Qwen/Qwen3-0.6B`：根据请求队列在 1–3 个副本之间自动扩缩；
- `unsloth/Llama-3.2-1B-Instruct`：固定 1 个副本。

每个副本使用 1 张 GPU，因此集群需要 2–4 张可调度 GPU。准备 4 张 GPU 可以覆盖完整扩缩容范围。

## 基于队列的自动扩缩容

Qwen 服务先按顶层的 `replicas: 1` 启动。控制器随后每 5 秒评估一次请求积压，并以每个副本平均 1 个等待请求为容量目标。每次触发最多增减 1 个副本，副本数上限为 3；只有当较低的容量建议持续 5 分钟后，控制器才会开始缩容。

## 部署

先安装 Foretoken 平台，再使用 pip 从仓库根目录安装 CLI：

```bash
pip install -e .
```

或使用 uv 创建并激活虚拟环境后安装：

```bash
uv venv
source .venv/bin/activate
uv pip install -e .
```

部署示例：

```bash
foretoken deploy examples/multi-model-quickstart
```

该命令会自动发现渲染配置中的全部模型，在服务状态变化时输出进度，并在当前配置就绪后退出。

观察 Qwen 副本变化：

```bash
kubectl get modelpool,modelgroup \
  --namespace foretoken-multi-model-demo \
  --watch
```

## 发送请求

```bash
FRONTEND_URL="$(foretoken endpoint examples/multi-model-quickstart)"
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
foretoken delete examples/multi-model-quickstart
```
