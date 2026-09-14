<!--
SPDX-License-Identifier: Apache-2.0
SPDX-FileCopyrightText: Copyright contributors to the Foretoken project
-->

# 对已有服务进行性能剖析

[English](profiling.md) | 简体中文

在已有模型服务处理请求时，用 PyTorch Profiler 采集一段 CPU/GPU 执行时间线。此功能处于实验阶段，需要源码安装，目前支持 NVIDIA GPU 上的 vLLM PyTorch profiler。

## 开始采集

使用已部署服务对应的 Kustomize 目录：

```bash
foretoken profile examples/quickstart \
  --profile-engine pytorch \
  --profile-duration 15s
```

命令只读取目录以定位已部署的服务，不重新应用配置。目录包含多个模型时，通过 `--model MODEL_ID` 选择一个。命令不会产生流量；采集期间通过正常的 Frontend 入口发送请求。

所选 ModelService 必须使用持久 RuntimeCache。维护中的快速开始示例已在 `cache.yaml` 中声明该存储，其他部署可参照[模型存储](../docs/model-storage_zh.md)。采集结果写入同一 RuntimeCache PVC 的 `profiles/` 目录。没有持久 RuntimeCache 的服务需要增加存储并重新部署后再采集。

runtime 在 profiler 启动后开始记录，到达指定时长后停止并导出文件，导出可能比记录耗时更长。正常完成不会停止模型推理。命令会输出 ProfileRun 名称，并在完成后输出结果所在的 RuntimeCache PVC 和路径。

| 参数 | 含义 |
|---|---|
| `--profile-engine pytorch` | 必填的 profiler 选择，目前只支持 PyTorch |
| `--profile-duration 15s` | 必填的记录时长，不含启动和导出时间 |
| `--model MODEL_ID` | 从多模型目录中选择一个模型 |
| `--timeout 10m` | CLI 等待进度的时间，不是 runtime 的采集时长 |

Ctrl-C 会请求取消并保留已有结果。终端断线或等待超时后，采集仍按原时限结束；可使用命令输出的查询指令查看进度。

## 查看结果

每个 runtime 在以下目录中保存一份 manifest 和原生 `.pt.trace.json` 文件：

```text
profiles/runs/<run-uid>/<runtime-id>/
```

默认的目录型快速开始示例中，`./data` 相对于 Kustomize 目录解析；当该目录可访问时，可在 checkout 的 `examples/quickstart/data/profiles/runs/` 下找到结果。其他部署使用命令输出的 PVC 和相对路径定位结果；数据目录可能位于集群中，而不在工作站上。

Profiling 会增加 CPU/GPU 开销，短窗口在高负载下仍可能产生很大文件。应使用规模较小的诊断部署和短窗口。原生 profiler 失败可能终止对应 runtime，因此服务需要允许这类中断。
