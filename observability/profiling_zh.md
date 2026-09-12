<!--
SPDX-License-Identifier: Apache-2.0
SPDX-FileCopyrightText: Copyright contributors to the Foretoken project
-->

# 对已有服务进行性能剖析

[English](profiling.md) | 简体中文

在模型处理请求时，用 PyTorch Profiler 采集一段 CPU/GPU 执行时间线。此功能处于实验阶段，需要源码安装，目前支持 NVIDIA GPU 上的 vLLM PyTorch profiler。

## 开始采集

使用已有诊断服务对应的 Kustomize 目录：

```bash
foretoken profile examples/quickstart \
  --profile-engine pytorch \
  --profile-duration 15s
```

命令只读取目录以定位服务，不重新部署。目录包含多个模型时，通过 `--model MODEL_ID` 选择一个。命令不会产生流量；采集期间通过正常的 Frontend 入口发送请求。

到达指定时长后，runtime 停止记录并导出文件，导出可能比记录耗时更长。正常完成不停止模型推理。命令先输出可供后续查询的 ProfileRun 名称，结束后输出结果所在的持久卷声明（PVC）及路径，不自动下载文件。

| 参数 | 含义 |
|---|---|
| `--profile-engine pytorch` | 必填的 profiler 选择，目前只支持 PyTorch |
| `--profile-duration 15s` | 必填的记录时长，不含启动和导出时间 |
| `--model MODEL_ID` | 从多模型目录中选择一个模型 |
| `--timeout 10m` | CLI 等待进度的时间，不是 runtime 的采集时长 |

Ctrl-C 请求取消，并保留已经产生的结果。终端断线或等待超时不会取消已接受的采集，runtime 仍会自动停止。不要因为命令不再等待就重复发起采集，应先用它输出的查询命令检查原运行状态。

## 部署前准备

这些准备在模型部署前完成，不需要每次采集重新操作：

1. 选择诊断命名空间，并在其中创建独立的产物 PVC。所有参与的 model-server Pod 都需要写权限；跨节点 Pod 使用支持 ReadWriteMany 的存储。不要复用模型缓存或 KV 存储的卷。
2. 复制 [profiling values 示例](../deploy/profiling-values.example.yaml)，替换为实际命名空间和 PVC 名，通过 `foretoken install -e . --values YOUR_VALUES_FILE` 进行[源码安装](../docs/custom-deployment_zh.md)。源码流程配套构建控制器、CRD 和 model-server，按集群需要提供 registry 参数。
3. 将模型部署到该命名空间并等待就绪。调用者需要创建、读取、修改 ProfileRun 的 Kubernetes 权限。CLI 使用当前 Kubernetes context。

该绑定会为命名空间内所有 model-server Pod 准备 profiler，并改变部署模板，因此应使用专门的诊断命名空间。采集命令不会为补装 profiler 而修改或重启 Pod。不需要先开启监控或 Alertmanager。

## 查看结果

每次运行在产物 PVC 中独立保存原生 `.pt.trace.json` 文件和 manifest。通过平台已有的存储访问方式取得文件，再用本地 Perfetto viewer 或其他兼容工具查看。manifest 分别记录请求停止采集和完成导出的时间，不包含客户端文件传输。

文件有效但没有 GPU kernel 活动时，结果说明窗口空闲，不据此判定 GPU 健康或繁忙。缺少预期 worker 文件或 trace 格式错误时，发布失败。取消后的结果可能不完整；后一次采集不会覆盖前一次目录。

Profiling 会增加 CPU/GPU 开销，短窗口在高负载下仍可能产生很大文件。当前命令采集所选服务已准备好的运行实例，不按请求数量抽样，也不限制 GPU 事件数或文件字节数。应使用规模较小的诊断部署和短窗口。原生 profiler 失败可能终止诊断 runtime，因此服务需要允许这类中断。删除命名空间或产物 PVC 可能删除保留结果。

部署后自动采集、benchmark 接入、延迟、请求抽样、Nsight Systems 和沐曦工具不属于当前单次采集接口。职责边界和后续拆分见[维护者设计与交付规划](../docs/development/profiling_zh.md)。
