<!--
SPDX-License-Identifier: Apache-2.0
SPDX-FileCopyrightText: Copyright contributors to the Foretoken project
-->

# 采集 NVIDIA Nsight Systems 时间线

[English](README.md) | 简体中文

本示例基于 Quick Start，在 `foretoken-nsight` 命名空间准备 Qwen3-0.6B 的 CUDA/NVTX 采集环境。需要源码安装的平台、NVIDIA GPU，以及按[性能剖析指南](../../observability/profiling_zh.md#nsight-systems)配置的可选诊断镜像。

```bash
foretoken deploy examples/nsight --timeout 20m
foretoken profile examples/nsight --profile-engine nsight --profile-duration 15s
```

命令显示 `Capturing` 时，通过前端发送推理请求；采集命令本身不产生流量。导出后模型继续服务，可再次采集。命令输出的 RuntimeCache 路径中，每个 runtime 保存 `capture.nsys-rep`、SQLite 导出文件和 `manifest.json`。

保存所需报告后，删除部署：

```bash
foretoken delete examples/nsight
```
