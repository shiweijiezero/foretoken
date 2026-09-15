<!--
SPDX-License-Identifier: Apache-2.0
SPDX-FileCopyrightText: Copyright contributors to the Foretoken project
-->

# 服务拥有的 Profiling 生命周期

[English](profiling.md) | 简体中文

当前实验实现对已有诊断 ModelService 执行一次有时长上限的 Torch 采集，不依赖 benchmark 或监控功能。执行命令和取得产物，请先看[操作指南](../../observability/profiling_zh.md)。

## 职责与执行路径

发起命令断线后，采集仍须停止并保留结果。因此，一次采集的身份和生命周期属于命名空间内的 `ProfileRun`，不属于命令进程，也不放进长期服务配置 `ModelService.spec`。

ProfileRun 操作使用 Kubernetes RBAC，内部 HTTP 沿用已有平台网络信任边界。

## 在采集前准备运行环境

Profiling 使用 ModelService 已解析的 RuntimeCache 绑定。ModelGroup 控制器将其 PVC 挂载为数据根目录，runtime 在该根目录下派生 `profiles/`。Profiling 不使用 KV offload 或 connector 卷，ProfileRun 也不拥有工作负载或存储。

没有持久 RuntimeCache 的 serving Group 不能参与采集，控制器会在启动原生 profiler 前报告错误。RuntimeCache 已回退到 Pod 本地临时目录时，runtime 会报告 profiling 不可用。这两种情况都不会创建其他卷，也不会把结果改写到临时存储。

Model-server 镜像内建 PyTorch 采集支持，[vLLM 补丁](../../data-plane/patches/vllm-python-profiling.patch)会报告原生启停错误，并允许后续再次采集。只有收到采集请求后才启动 profiler。

## 采集身份与恢复

API 在创建后固定目标、采集工具和时长。动作从 `Capture` 推进到 `Finish` 或 `Cancel`，取消不可撤销。即使资源同名，Kubernetes UID 也能区分不同采集。

启动原生采集前，控制器先持久化删除 finalizer，并在 ProfileRun status 中保存执行计划。计划固定 serving generation、RuntimeCache claim、Group、Pod 和 runtime 参与者。控制器重启后读取原计划，不重新选择替代实例或存储。

状态依次经过 `Starting`、`Capturing`、`Stopping`，最终为 `Succeeded`、`Failed` 或 `Cancelled`。只有全部参与实例都在采集时，运行才报告 `Capturing`；成功也需要核对全部预期参与者。发出 HTTP 操作不代表采集完成。Runtime 拒绝竞争采集，同一 UID 的重试不重启窗口，也不延长时限。

参与实例失联、被替换或服务实例集合改变时，控制器取消剩余工作，报告覆盖范围或停止确认问题。Pod 消失不等于引擎已停止；无法确认时保留 finalizer 和恢复计划供管理员诊断。删除活动运行也会请求取消。已封存产物不以 ProfileRun 为 owner，不随运行记录被垃圾回收。

## Runtime 时限

原生 utility 在 supervisor 拥有的任务中运行，不依赖 HTTP 请求存活。取消只改变期望动作，不丢弃正在执行的 utility。

调用者显式指定采集时长；原生启动成功后，runtime 才开始计时。原生启动和停止/flush 分别使用 30 秒、120 秒预算。提前 `Finish` 可以结束较短窗口。CLI 默认等待 10 分钟，这个观察期限不会改变 runtime 的时限。

没有活动采集时，runtime 保持正常的请求排空和退出顺序。原生 utility 失败或超时后，profiler 状态可能不确定。诊断 runtime 先关闭新请求准入，再沿已有引擎进程组关闭流程确认退出，之后释放 utility 任务。这可能中断该诊断服务的推理请求。不能确认终止时报告问题，不声称采集成功；部分输出保留供存储管理者排查。

## 先封存，再发布结果

每个 runtime 都在 RuntimeCache PVC 提供的持久数据根目录下写入：

```text
<data-root>/profiles/.staging/<runtime-id>/
<data-root>/profiles/runs/<run-uid>/<runtime-id>/
```

开始前要求 staging 为空，不删除尚未处理的残留输出。原生停止/flush 完成后，成功采集必须为每个预期 worker 提供一份合法 Torch trace。GPU 活动单独报告，合法的空闲窗口不导致发布失败。取消时保留已有输出，但不将其视为完整采集。

停止和 flush 完成后，supervisor 写入 manifest，在同一文件系统内原子重命名 staging 目录，再发布结果引用。原子性属于单个参与者，不是分布式事务。后续采集使用不同的 run 目录。封存失败不发布成功，也不删除可恢复数据。

Manifest 的 `startedAtUnixMs` 在原生启动后记录，`recordingEndedAtUnixMs` 表示发起停止，`exportedAtUnixMs` 表示停止/flush 返回。不同 worker 的实际停止时间可能略有差异，这些控制时间戳不代表精确的 GPU 事件边界。ProfileRun 的 `finishedAt` 是控制器观察到完成的时间。

## Benchmark 协调

`benchmarks/profiling` 通过 `foretoken profile` 共用的 `foretoken.profiling.ProfileRun` 客户端协调一次生成式负载。它不启动原生 profiler，也不拥有服务资源。此模式只接受已有部署，避免 benchmark 清理时删除 RuntimeCache 产物。

EvalScope 适配层让准备好的 HTTP 请求等待同一个采集启动任务，此等待发生在请求延迟计时之前。只有全部参与者报告 `Capturing` 才放行；已经结束的窗口会终止请求发送。请求调度、取消和事件循环关闭仍由 EvalScope 负责。benchmark 上下文提交取消前，EvalScope 会等待线程池退出，因此 Ctrl-C 发生在创建请求途中时也能按返回的 UID 取消。

负载正常结束时提交 `Finish` 并等待导出，异常退出时提交 `Cancel`。Runtime 时限仍独立生效。本地 `profile.json` 与 HTTP 结果一起保留运行身份和客户端观察时间；实际录制覆盖范围仍以原生 manifest 和 trace 为准。

## 上游参考

- [vLLM profiling](https://docs.vllm.ai/en/stable/contributing/profiling/)：引擎配置、输出和诊断开销。
- [PyTorch profiler](https://docs.pytorch.org/docs/stable/profiler.html)：原生采集与 trace 导出。
