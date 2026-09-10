<!--
SPDX-License-Identifier: Apache-2.0
SPDX-FileCopyrightText: Copyright contributors to the Foretoken project
-->

# 服务拥有的 Profiling 生命周期

[English](profiling.md) | 简体中文

当前实验实现对已有诊断 ModelService 执行一次有时长上限的 Torch 采集，不依赖 benchmark 或监控功能。准备环境、执行命令和取得产物，请先看[操作指南](../../observability/README_zh.md#单次-profiling实验性需源码构建)。

## 职责与执行路径

发起命令断线后，采集仍须停止并保留结果。因此，一次采集的身份和生命周期属于命名空间内的 `ProfileRun`，不属于命令进程，也不放进长期服务配置 `ModelService.spec`。

```text
普通负载 ── 公开 Frontend ─────────> model-server
                                          │
foretoken profile ── Kubernetes API        │
                          │               │
                      ProfileRun          │
                          │               │
                    现有控制器 ────────────┘
                     内部 HTTP            │
                                    runtime supervisor
                                          │
                                     vLLM / Torch
                                          │
                                      产物 PVC
```

CLI 创建并观察运行，Ctrl-C 请求取消。现有控制面管理器选定服务实例，通过 model-server 已有的内部监听接口发送采集意图，并发布观察到的状态。Runtime supervisor 负责原生启动、自动停止、导出和失败处置。平台负责独立产物 PVC 及其保留周期。

命令不产生请求、不修改服务配置、不部署 benchmark Job、不开放公开 profiling 端口，也不从 Pod 拷贝文件。推理 token 不等于 Kubernetes 采集权限。ProfileRun 使用 Kubernetes RBAC；内部 HTTP 沿用已有平台网络信任边界，不提供 Pod 之间的逐用户授权。

## 在采集前准备运行环境

平台通过 `profiling.artifactClaims` 将诊断命名空间绑定到已有 PVC。ModelGroup 控制器在正常 Pod 模板中添加挂载和运行身份。这会改变部署，因此应在部署诊断服务前完成；ProfileRun 本身不拥有或修改工作负载与 PVC。

Runtime 启动时生成新的进程身份，配置已安装引擎的 Torch profiler，将未压缩 trace 写入独占 staging 目录。Model-server 镜像应用 [vLLM Python 修复补丁](../../data-plane/patches/vllm-python-profiling.patch)，使启停错误能够返回调用方，并支持互相独立的后续采集。Rust 源码子模块不是镜像里实际运行的 Python 引擎；后者必须能应用该补丁，或已包含完全匹配的实现。

## 采集身份与恢复

以下是 CLI 提交的 API 意图，用户不需要为每次采集手写这份 YAML：

```yaml
apiVersion: inference.foretoken.io/v1alpha1
kind: ProfileRun
metadata:
  generateName: profile-
  namespace: foretoken-diagnostic
spec:
  modelServiceRef:
    name: diagnostic-model
  duration: 15s
  action: Capture
```

API 在创建后固定目标和时长。动作从 `Capture` 推进到 `Finish` 或 `Cancel`，取消不可撤销。即使资源同名，Kubernetes UID 也能区分不同采集。

启动原生采集前，控制器先持久化删除 finalizer 和运行拥有的不可变 ConfigMap 执行计划。它复用已有路由辅助逻辑，解析 ModelService 已提交的 serving generation，并核对 Pod → ReplicaSet → Deployment → ModelGroup 的归属链。计划记录固定的服务、Group、Pod 和 runtime 身份。控制器重启后读取原计划，不重新选择替代实例。

状态依次经过 `Starting`、`Capturing`、`Stopping`，最终为 `Succeeded`、`Failed` 或 `Cancelled`。只有全部参与实例都在采集时，运行才报告 `Capturing`；成功也需要核对全部预期参与者。发出 HTTP 操作不代表采集完成。Runtime 拒绝竞争采集，同一 UID 的重试不重启窗口，也不延长时限。

参与实例失联、被替换或服务实例集合改变时，控制器取消剩余工作，报告覆盖范围或停止确认问题。Pod 消失不等于引擎已停止；无法确认时保留 finalizer 和恢复计划供管理员诊断。删除活动运行也会请求取消。已封存产物不以 ProfileRun 为 owner，不随运行记录被垃圾回收。

## Runtime 时限

状态更新只短暂持锁。原生 utility 在 supervisor 拥有的任务中运行，不占用该锁，也不依赖 HTTP 请求任务存活。取消只改变期望动作，不丢弃正在执行的 utility。

ProfileRun API 定义默认 15 秒采集时长；原生启动成功后，runtime 才开始计时。原生启动和停止/flush 分别使用 30 秒、120 秒预算。提前 `Finish` 可以结束较短窗口。CLI 默认等待 10 分钟，这个观察期限不会改变 runtime 的时限。

原生 utility 失败或超时后，profiler 状态可能不确定。诊断 runtime 先关闭新请求准入，再沿已有引擎进程组关闭流程确认退出，之后释放 utility 任务。这可能中断该诊断服务的推理请求。不能确认终止时报告问题，不声称采集成功；部分输出保留供存储管理者排查。

## 先封存，再发布结果

Runtime 使用平台提供的独立、共享可写产物 PVC，不复用模型缓存或 KV 存储：

```text
<artifact-volume>/.staging/<runtime-id>/
<artifact-volume>/runs/<run-uid>/<runtime-id>/
```

开始前要求 staging 为空，不删除尚未处理的残留输出。原生停止/flush 完成后，成功采集必须为每个预期 worker 提供一份包含 GPU kernel 活动的合法 Torch trace。逐事件读取避免将整个大文件加载进内存。取消时保留已有输出，但不将其视为完整采集。

Supervisor 写入并 flush manifest，在同一文件系统内重命名整个 staging 目录，再 flush 目标目录，随后发布结果引用。原子性属于单个参与者，不是分布式事务。后续采集重建 staging，使用不同 run 目录。封存失败不发布成功，也不删除可恢复数据。

Manifest 的 `startedAtUnixMs` 在原生启动后记录，`stoppedAtUnixMs` 在原生停止/flush 后记录；两者之差包含导出，不能解释为纯记录时长。ProfileRun 的 `finishedAt` 是控制器观察到完成的时间。命令返回 PVC 和路径，不代表已下载到本机。删除命名空间或 PVC 仍由存储管理者负责，也可能删除产物。

## 验证范围

真实 Kubernetes 实验使用 vLLM 0.26.0、单 worker NVIDIA A100 服务，验证了两次独立采集、Ctrl-C 取消后保留输出、CLI 进程退出后自动完成、控制器重启恢复，以及采集后的正常推理。Perfetto 能解析 trace，GPU kernel 数量与原始 JSON 一致；CPU slice 重叠的导入提示仍存在，没有修改原文件来隐藏提示。

原生 utility 卡死、存储封存失败、多 worker 覆盖和性能开销仍需硬件实验。已有直接生命周期检查不能代替这些验证。Benchmark 接入、延迟、采样上限、重复窗口、Nsight 和沐曦不属于当前实现，也不是独立命令的使用前提。

## 上游参考

- [vLLM profiling](https://docs.vllm.ai/en/stable/contributing/profiling/)：引擎配置、输出和诊断开销。
- [PyTorch profiler](https://docs.pytorch.org/docs/stable/profiler.html)：原生采集与 trace 导出。
- [Dynamo Profiler](https://docs.dynamo.nvidia.com/dynamo/dev/knowledge-base/modular-components/profiler/overview)：部署性能标定与有限 runtime trace 采集的产物和职责不同。
