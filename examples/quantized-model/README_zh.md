<!-- SPDX-License-Identifier: Apache-2.0 -->
<!-- SPDX-FileCopyrightText: Copyright contributors to the Foretoken project -->

# 部署量化模型

[English](README.md) | 简体中文

这些示例使用预量化 checkpoint，或在加载普通权重时进行量化，部署 Qwen2.5-0.5B-Instruct。每个目录都是完整的 Kustomize 部署，包含独立的 namespace、RuntimeCache、FrontendService 和 ModelService。两个示例可以分别部署和清理，同时运行也不会发生资源名称冲突。

部署前，先从当前源码目录[构建并安装平台](../../docs/custom-deployment_zh.md)：

```bash
foretoken install -e .
```

每套部署中的模型和前端合计申请 1 张 GPU、3 核 CPU 和 9 GiB 主机内存，此外还需为 Foretoken 平台预留资源。

## NVIDIA GPU 上的 AWQ

该示例加载预量化 checkpoint `Qwen/Qwen2.5-0.5B-Instruct-AWQ`，激活值使用 FP16，需要 1 张支持对应 vLLM 量化路径的 NVIDIA GPU。

```bash
foretoken deploy examples/quantized-model/awq --timeout 20m
```

使用完后只清理 AWQ 部署：

```bash
foretoken delete examples/quantized-model/awq
```

## NVIDIA GPU 上的 BitsAndBytes 4-bit

该示例加载 `Qwen/Qwen2.5-0.5B-Instruct`，并在加载时使用 BitsAndBytes 进行 4-bit 量化，需要 1 张支持对应 vLLM 量化路径的 NVIDIA GPU。加载时量化不会生成新的 checkpoint。

```bash
foretoken deploy examples/quantized-model/bitsandbytes --timeout 20m
```

使用完后只清理 BitsAndBytes 部署：

```bash
foretoken delete examples/quantized-model/bitsandbytes
```

## 存储与请求

每个示例的 `cache.yaml` 都使用项目根目录的 `data/`。远程集群需要在所部署示例中，将 `directory: ../../../data` 改为目标节点可访问的绝对路径，详见[模型存储](../../docs/model-storage_zh.md)。

部署后使用 `foretoken endpoint` 获取对应前端地址，并在 OpenAI API 兼容格式的请求中填写该节列出的模型标识。请求格式和 Gateway 域名配置见[单模型快速开始](../quickstart/README_zh.md)。清理部署后，已经下载的模型文件仍保留在目录缓存中。
