<!-- SPDX-License-Identifier: Apache-2.0 -->
<!-- SPDX-FileCopyrightText: Copyright contributors to the Foretoken project -->

# 单模型快速开始

[English](README.md) | [中文](README_zh.md)

如需运行双模型并验证自动扩缩容，请参阅[多模型快速开始](../multi-model-quickstart/README_zh.md)。

本示例部署一个前端服务和一个 `Qwen/Qwen3-0.6B` 模型副本，使用 1 张 GPU。

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
