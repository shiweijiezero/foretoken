<!--
SPDX-License-Identifier: Apache-2.0
SPDX-FileCopyrightText: Copyright contributors to the Foretoken project
-->

# 性能剖析

[可观测性](README_zh.md) | [English](profiling.md)

Foretoken 不管理性能剖析流程。应先通过指标和 Dashboard 定位可复现的瓶颈，再通过模型运行环境和硬件平台执行受控性能剖析。

## 选择剖析工具

- **PyTorch Profiler** 用于查看 model-server 进程中的算子、CPU、CUDA 和内存活动。
- **Nsight Systems** 用于查看服务进程与 GPU 之间的主机、设备和通信时间线。
- **Nsight Compute** 用于分析聚焦 CUDA 工作负载的详细 kernel 行为。

使用 model-server 镜像和硬件平台支持的剖析入口。Foretoken 不增加统一的剖析参数，也不会修改 runtime 启动命令来接入这些工具。

## 执行可复现的调查

1. 固定一个模型、副本数量、硬件规格和 runtime 配置。
2. 使用受控请求负载，固定并发数、prompt 分布和输出长度上限。
3. 将预热阶段与正式采集阶段分开记录。
4. 只采集能够回答瓶颈问题的短时间窗口；剖析会引入开销并改变服务行为。
5. 将剖析结果与[可观测性 Dashboard](README_zh.md)中的 Frontend、model-server、加速器和 RuntimeCache 指标对照。

应随结果记录模型标识、Foretoken 和后端镜像版本、Kubernetes 资源请求、GPU 类型、并行参数、请求负载和剖析工具版本。剖析产物应存放在平台的实验或对象存储中，不要提交到代码仓库。

## 分析与清理

利用剖析结果区分计算饱和、通信延迟、CPU 调度、模型加载以及缓存或文件系统影响。准备应用改动前，使用相同负载和指标确认改动效果。

剖析会话、临时调试设置和采集的 trace 由创建它们的运维人员负责。调查结束后通过 runtime 或平台流程清理；`foretoken uninstall` 不管理剖析产物或外部剖析器配置。
