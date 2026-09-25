<!-- SPDX-License-Identifier: Apache-2.0 -->
<!-- SPDX-FileCopyrightText: Copyright contributors to the Foretoken project -->

# 部署量化模型

[English](README.md) | 简体中文

这些示例使用预量化 checkpoint，或在加载普通权重时进行量化，部署 Qwen2.5-0.5B-Instruct。每个目录都是完整的 Kustomize 部署，包含独立的 namespace、RuntimeCache、FrontendService 和 ModelService。各套部署可以分别创建和清理，同时运行也不会发生资源名称冲突。目录中还提供相同基础模型的 BF16 参考部署，用于概率分布对比。

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

## 与 BF16 比较

在仓库根目录运行，将 bitsandbytes 与相同模型的非量化版本比较，两端均使用 BF16 计算精度：

```bash
foretoken eval compare examples/quantized-model/bitsandbytes \
  --reference examples/quantized-model/bf16 --output local
```

命令从部署配置读取模型与 tokenizer，依次执行参考和候选模型。已有部署直接复用，临时部署在使用后删除。若希望位宽图同时显示 BF16 自身的对照结果，将候选目录换成 `--candidates examples/quantized-model/candidates.jsonl`。

示例已允许返回完整概率分布。若已有部署使用旧版示例配置，比较前先用 `foretoken deploy PATH` 应用新版文件。AWQ 示例使用 FP16 激活值，与 BF16 比较时也包含计算精度差异。指标和自定义候选见[模型概率分布对比](../../benchmarks/docs/eval/fidelity_zh.md)。

## 存储与请求

每个示例的 `cache.yaml` 都使用项目根目录的 `data/`。远程集群需要在所部署示例中，将 `directory: ../../../data` 改为目标节点可访问的绝对路径，详见[模型存储](../../docs/model-storage_zh.md)。

部署后使用 `foretoken endpoint` 获取对应前端地址，并在 OpenAI API 兼容格式的请求中填写该节列出的模型标识。请求格式和 Gateway 域名配置见[单模型快速开始](../quickstart/README_zh.md)。清理部署后，已经下载的模型文件仍保留在目录缓存中。
