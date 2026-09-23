<!--
SPDX-License-Identifier: Apache-2.0
SPDX-FileCopyrightText: Copyright contributors to the Foretoken project
-->

# 采集 NVIDIA Nsight Systems 时间线

[English](README.md) | 简体中文

本示例基于 Quick Start，在 `foretoken-nsight` 命名空间采集 Qwen3-0.6B 的 CUDA/NVTX 活动。需要 NVIDIA GPU 和[性能剖析指南](../../../observability/profiling_zh.md#nsight-systems)中的诊断镜像。示例沿用快速开始的[模型存储](../../../docs/model-storage_zh.md)：本地 k3d 使用仓库的 `data/` 挂载，远端集群配置节点目录或 PVC。

运行一段短负载并采集：

```bash
pip install -e .
foretoken perf examples/profile/nsight \
  --profile --profile-engine nsight --profile-duration 15s \
  --num-prompts 2 --max-tokens 128 --output local
foretoken profile view
```

选择 Nsight 时间线，点击“Open in NVIDIA Nsight Systems”即可在浏览器中使用 NVIDIA 官方查看器。临时评测服务清理后，采集文件仍可访问。

不再需要时，删除示例保留的资源：

```bash
foretoken delete examples/profile/nsight
```
