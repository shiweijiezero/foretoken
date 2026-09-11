<!-- SPDX-License-Identifier: Apache-2.0 -->
<!-- SPDX-FileCopyrightText: Copyright contributors to the Foretoken project -->

# 单模型快速开始

[English](README.md) | [中文](README_zh.md)

如需运行双模型并验证自动扩缩容，请参阅[多模型快速开始](../multi-model-quickstart/README_zh.md)。

本示例部署一个前端服务和一个 `Qwen/Qwen3-0.6B` 模型副本。工作负载请求 1 张 GPU、8 个 CPU 和 52 GiB 内存；还需为平台预留额外容量。模型文件和运行时缓存存放在 `cache.yaml` 配置的 `./data` 目录中。

在 [`model.yaml`](model.yaml) 的 `ModelService` 中配置模型、副本数、资源和并行参数，在 [`cache.yaml`](cache.yaml) 的 `RuntimeCache` 中配置运行时缓存，在 [`frontend.yaml`](frontend.yaml) 的 `FrontendService` 中配置前端。Foretoken 会自动创建所需的 Kubernetes 工作负载。

## 部署

从源码[安装平台](../../docs/custom-deployment_zh.md)并准备[模型存储](../../docs/model-storage_zh.md)，再从仓库根目录部署：

```bash
foretoken deploy examples/quickstart --timeout 20m
```

该命令会在服务状态变化时输出进度，并在当前配置就绪后退出。

## 发送请求

解析前端服务 URL：

```bash
FRONTEND_URL="$(foretoken endpoint examples/quickstart)"
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
foretoken delete examples/quickstart
```
