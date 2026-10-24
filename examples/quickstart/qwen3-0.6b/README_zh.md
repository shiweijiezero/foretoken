<!-- SPDX-License-Identifier: Apache-2.0 -->
<!-- SPDX-FileCopyrightText: Copyright contributors to the Foretoken project -->

# 单模型快速开始

[English](README.md) | [中文](README_zh.md)

如需运行双模型并验证自动扩缩容，请参阅[多模型快速开始](../multi-model/README_zh.md)。

预量化权重和在线量化的用法见[部署量化模型](../../feature/quantized-model/README_zh.md)。

本示例部署一个前端服务和一个 `Qwen/Qwen3-0.6B` 模型副本。工作负载请求 1 张 GPU、8 个 CPU 和 52 GiB 内存；还需为平台预留额外容量。模型使用平台默认的[数据存储](../../../docs/features/model-storage_zh.md)，与多模型示例复用下载，运行时缓存则按实例隔离。

在 [`model.yaml`](model.yaml) 的 `ModelService` 中配置模型、副本数、资源、并行方式和[推理参数](../../../docs/features/inference-parameters_zh.md)，在 [`frontend.yaml`](frontend.yaml) 的 `FrontendService` 中配置前端。Foretoken 会自动创建所需的 Kubernetes 工作负载。

## 部署

安装 [Foretoken 平台](../../../README_zh.md#2-安装-kubernetes-平台)后，在仓库根目录部署：

```bash
foretoken deploy examples/quickstart/qwen3-0.6b --timeout 20m
```

不传 `--name` 时，实例名采用 [`frontend.yaml`](frontend.yaml) 中 `FrontendService` 的 `metadata.name`，本例为 `qwen3-0-6b`。命令在当前 kubeconfig 的命名空间中创建该实例，并等待服务就绪。添加 `--name candidate-a` 则部署名为 `candidate-a` 的独立实例，后续命令也使用这个名字。

## 发送请求

获取前端服务 URL 和请求所需的 Host：

```bash
FRONTEND_URL="$(foretoken endpoint qwen3-0-6b)"
REQUEST_HOST="$(foretoken endpoint qwen3-0-6b --host)"
```

发送 OpenAI API 兼容格式的请求：

```bash
curl "$FRONTEND_URL/v1/chat/completions" \
  -H "Host: $REQUEST_HOST" \
  -H 'Content-Type: application/json' \
  -d '{
    "model": "Qwen/Qwen3-0.6B",
    "messages": [{"role": "user", "content": "Reply with: Foretoken is ready"}],
    "temperature": 0
  }'
printf '\n'
```

## 清理

```bash
foretoken delete qwen3-0-6b
```
