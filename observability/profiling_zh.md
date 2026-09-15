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

## 同时运行 benchmark 和采集

从源码安装 benchmark 依赖（`pip install -e '.[bench]'`）后，可对已部署的诊断服务用一条命令发请求并采集：

```bash
foretoken bench examples/quickstart \
  --profile --profile-engine pytorch --profile-duration 15s \
  --number 2 --max-tokens 128 --output local
```

命令准备好负载，等待全部选中 runtime 报告 `Capturing`，再通过正常的 Frontend 发送请求。如果观察到就绪前窗口已结束，会报错且不发送请求。请求完成后，命令请求 `Finish` 并等待导出；采集窗口先结束不会截断 benchmark。此模式使用一个生成式负载和 `--rate -1`，不支持仅提供 URL 的服务、轨迹回放、参数扫描或多个数据集。

`--wait-timeout` 分别限制采集启动与完成阶段的等待时间。Ctrl-C 或负载执行失败会请求取消；采集未成功时命令也会报错。已有服务和 RuntimeCache 结果会保留，无需转发端口或添加 profiling 专用的服务 YAML。

启用本地输出时，`profile.json` 记录 ProfileRun 身份、最后观察到的状态、采集就绪观察时间和请求时间。这些是客户端观察，不是精确的 GPU 事件边界；实际录到了什么，要查看原生 trace 和 manifest。比较延迟、吞吐量时，应另跑一次不启用 profiling 的 benchmark。

## 查看结果

每个 runtime 在以下目录中保存一份 manifest 和原生 `.pt.trace.json` 文件：

```text
profiles/runs/<run-uid>/<runtime-id>/
```

Quick Start 示例的结果位于项目根目录的 `data/profiles/runs/`。其他部署按命令输出的 PVC 和相对路径查找。

Profiling 会增加 CPU/GPU 开销，短窗口在高负载下仍可能产生很大文件。应使用规模较小的诊断部署和短窗口。原生 profiler 失败可能终止对应 runtime，因此服务需要允许这类中断。
