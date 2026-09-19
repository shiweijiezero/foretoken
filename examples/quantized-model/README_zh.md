<!-- SPDX-License-Identifier: Apache-2.0 -->
<!-- SPDX-FileCopyrightText: Copyright contributors to the Foretoken project -->

# 部署量化模型

[English](README.md) | 简体中文

使用预量化 checkpoint，或在加载普通权重时进行量化，部署 Qwen2.5-0.5B-Instruct。每种方案都有完整的 `model.yaml`，命名空间、前端和缓存配置共用 `shared/`。

| 配置目录 | 权重加载方式 | 硬件 |
| --- | --- | --- |
| [`awq/`](awq/model.yaml) | 加载预量化 AWQ checkpoint，使用 FP16 激活值 | NVIDIA A100 |
| [`bitsandbytes/`](bitsandbytes/model.yaml) | 加载普通 checkpoint，加载时进行 4-bit 量化 | NVIDIA A100 |
| [`torchao-metax/`](torchao-metax/model.yaml) | 加载普通 checkpoint，以 INT8 保存权重，线性层使用浮点计算 | MetaX C500 |

沐曦配置在执行每个线性层时反量化，减少的是权重存储，而非提供 INT8 矩阵运算加速。加载时量化不会导出新的 checkpoint。

## 安装与选择配置

使用 `foretoken install -e .` 从当前源码[安装平台](../../docs/custom-deployment_zh.md)。model-server 构建会为所选加速器准备量化依赖。沐曦环境参照[沐曦部署指南](../../docs/metax-deployment_zh.md)。

在仓库根目录选择一组配置：

```bash
# 预量化 AWQ
EXAMPLE=examples/quantized-model/awq
MODEL=Qwen/Qwen2.5-0.5B-Instruct-AWQ

# 或：A100 上的 BitsAndBytes 4-bit
# EXAMPLE=examples/quantized-model/bitsandbytes
# MODEL=Qwen/Qwen2.5-0.5B-Instruct

# 或：C500 上的 TorchAO INT8
# EXAMPLE=examples/quantized-model/torchao-metax
# MODEL=Qwen/Qwen2.5-0.5B-Instruct
```

各方案沿用模型配置中的上下文长度，模型与前端合计申请 1 张 GPU、3 核 CPU 和 9 GiB 主机内存，平台另需资源。它们用于切换同一个服务，不同时部署。

所有方案共用项目根目录的 `data/`。远程集群在 [`shared/cache.yaml`](shared/cache.yaml) 中填写节点可访问的绝对路径，详见[模型存储](../../docs/model-storage_zh.md)。

## 部署并发送请求

```bash
foretoken deploy "$EXAMPLE" --timeout 20m
FRONTEND_URL="$(foretoken endpoint "$EXAMPLE")"

curl --fail-with-body "$FRONTEND_URL/v1/chat/completions" \
  -H 'Content-Type: application/json' \
  --data-binary @- <<EOF
{
  "model": "$MODEL",
  "messages": [{"role": "user", "content": "Explain quantization in one sentence."}],
  "max_tokens": 64,
  "temperature": 0
}
EOF
```

Gateway 模式的域名和 Host 请求头配置见根目录快速开始。模型参数直接修改所选方案的 `model.yaml`，原生参数用法见[推理参数](../../docs/inference-parameters_zh.md)。

## 清理

```bash
foretoken delete "$EXAMPLE"
```

命名空间和服务资源会被删除，目录缓存中的已下载文件保留。
