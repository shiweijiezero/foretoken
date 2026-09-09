<!-- SPDX-License-Identifier: Apache-2.0 -->
<!-- SPDX-FileCopyrightText: Copyright contributors to the Foretoken project -->

# 单模型快速开始

[English](README.md) | [中文](README_zh.md)

如需运行双模型并验证自动扩缩容，请参阅[多模型快速开始](../multi-model-quickstart/README_zh.md)。

本示例部署一个前端服务和一个 `Qwen/Qwen3-0.6B` 模型副本。工作负载请求 1 张 GPU、8 个 CPU 和 52 GiB 内存；还需为平台预留额外容量。示例中的 `RuntimeCache` 使用 `./data` 保存预加载模型文件和运行时缓存。部署命令会创建静态目录 PV。k3d 必须将目录绑定到节点；普通 Kubernetes 需填写已准备的绝对节点目录，多节点时必须是所有节点可访问的同一共享目录。删除 `directory` 并设置 `initialSize` 可改用动态 PVC，具体权限、目录准备和保留规则见[缓存指南](../../docs/development/runtime-cache_zh.md)。

在 [`model.yaml`](model.yaml) 的 `ModelService` 中配置模型、副本数、资源和并行参数，在 [`cache.yaml`](cache.yaml) 的 `RuntimeCache` 中配置运行时缓存，在 [`frontend.yaml`](frontend.yaml) 的 `FrontendService` 中配置前端。Foretoken 会自动创建所需的 Kubernetes 工作负载。

目录模式需要当前源码 CLI 和匹配镜像，不适用于已发布的 0.0.2 包。

## 部署

先完成根目录[快速开始](../../README_zh.md)中的平台安装。部署前目录必须存在。如果使用 k3d，请按 [k3d 指南](../../docs/k3d-deployment_zh.md) 在创建集群时将目录绑定到节点：

```bash
mkdir -p examples/quickstart/data
foretoken deploy examples/quickstart
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
