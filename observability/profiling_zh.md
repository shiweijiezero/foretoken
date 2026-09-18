<!--
SPDX-License-Identifier: Apache-2.0
SPDX-FileCopyrightText: Copyright contributors to the Foretoken project
-->

# 性能剖析

[English](profiling.md) | 简体中文

使用 PyTorch Profiler 查看模型推理的 CPU/GPU 执行时间线。支持 NVIDIA 和[沐曦 GPU](../docs/metax-deployment_zh.md) 上的 vLLM，需使用[源码安装](../docs/custom-deployment_zh.md)的 CLI 和平台。采集结果使用持久 RuntimeCache 保存，快速开始示例已配置好该存储。

## 同时运行 benchmark 和采集

从仓库根目录执行：

```bash
pip install -e '.[bench]'
foretoken bench examples/quickstart \
  --profile --profile-engine pytorch --profile-duration 15s \
  --number 2 --max-tokens 128 --output local
```

此模式支持 Kustomize 部署中的单个生成式负载，使用默认的 `--rate -1`。

## 部署并采集外部流量

```bash
foretoken deploy examples/quickstart \
  --profile --profile-engine pytorch --profile-duration 15s
```

服务就绪后开始采集，请求由外部发送；采集结束后服务继续运行。多模型部署用 `--model MODEL_ID` 选择采集对象。`--profile-duration` 设置最长记录时间，评测负载提前结束时也会停止采集。

## 查看结果

在本地电脑使用目标集群的 kubeconfig 执行：

```bash
foretoken profile view
```

打开打印的网址，浏览采集目录及子目录，点击 trace 在 Perfetto 中查看。浏览器需能访问 `ui.perfetto.dev`。按 Ctrl+C 关闭查看器，文件会保留。

不再需要该部署及采集记录时清理：

```bash
foretoken delete examples/quickstart
```

Profiling 会增加开销，延迟和吞吐量对比请使用不带 `--profile` 的评测。
