<!--
SPDX-License-Identifier: Apache-2.0
SPDX-FileCopyrightText: Copyright contributors to the Foretoken project
-->

# 对已有服务进行性能剖析

[English](profiling.md) | 简体中文

在已有模型服务处理请求时采集一段执行时间线。此功能处于实验阶段，需要源码安装，支持 NVIDIA GPU 上 vLLM 的 PyTorch Profiler 或 NVIDIA Nsight Systems。

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
| `--profile-engine pytorch` 或 `nsight` | 必填的工具选择，需要与服务已准备的工具一致 |
| `--profile-duration 15s` | 必填的记录时长，不含启动和导出时间 |
| `--model MODEL_ID` | 从多模型目录中选择一个模型 |
| `--timeout 10m` | CLI 等待进度的时间，不是 runtime 的采集时长 |

Ctrl-C 会请求取消并保留已有结果。终端断线或等待超时后，采集仍按原时限结束；可使用命令输出的查询指令查看进度。

## Nsight Systems

Nsight Systems 采集一个 runtime 的引擎进程树中的 CUDA 和 NVTX 事件。它与 PyTorch 是两种采集方式，需要在模型启动前选择；更改选择会重新部署模型进程。本接入不启用 CPU 采样或 GPU 硬件计数器，也不要求特权 Pod。

### 准备诊断镜像

按[源码部署](../docs/custom-deployment_zh.md)构建 model-server 镜像。将 `MODEL_SERVER_IMAGE` 设为该镜像，`NSIGHT_IMAGE` 设为集群可访问的目标镜像名，再构建可选的 Linux x86_64 诊断镜像：

```bash
docker build -f deploy/inference-engines/nsight/Dockerfile \
  --build-arg MODEL_SERVER_IMAGE="$MODEL_SERVER_IMAGE" \
  -t "$NSIGHT_IMAGE" deploy/inference-engines/nsight
docker push "$NSIGHT_IMAGE"
```

Dockerfile 安装固定版本 Nsight Systems 2025.3.2。在平台 values 中配置该镜像：

```yaml
runtime:
  vllm:
    nsightImage: YOUR_NSIGHT_IMAGE
```

将 `YOUR_NSIGHT_IMAGE` 替换为已推送的镜像地址，再通过源码平台安装流程应用这份 values。仅选择 Nsight 的模型使用诊断镜像，普通服务继续使用原有运行镜像。

### 部署与采集

维护中的 overlay 已选择 `spec.profiling.engine: nsight` 并声明持久 RuntimeCache：

```bash
foretoken deploy examples/nsight --timeout 20m
foretoken profile examples/nsight --profile-engine nsight --profile-duration 15s
```

命令显示 `Capturing` 时发送请求。Nsight 在完成前导出报告，模型继续服务；再次执行 profile 命令可独立采集下一段。已有 PyTorch 服务需要先设置 `spec.profiling.engine: nsight` 并重新部署；工具不匹配的采集请求会在记录前被拒绝。

用 Nsight Systems 打开 `capture.nsys-rep`，或对复制出的报告执行 `nsys stats capture.nsys-rep` 查看统计。成功采集还保留 `capture.sqlite` 供分析。本功能不包含 Nsight Compute 的 kernel 硬件计数器分析。

## 查看结果

每个 runtime 在以下目录中保存一份 manifest 和原生报告：PyTorch 为 `.pt.trace.json`，Nsight 为 `.nsys-rep`。

```text
profiles/runs/<run-uid>/<runtime-id>/
```

Quick Start 示例的结果位于项目根目录的 `data/profiles/runs/`。其他部署按命令输出的 PVC 和相对路径查找。

Profiling 会增加 CPU/GPU 开销，短窗口在高负载下仍可能产生很大文件。应使用规模较小的诊断部署和短窗口。原生 profiler 失败可能终止对应 runtime，因此服务需要允许这类中断。
