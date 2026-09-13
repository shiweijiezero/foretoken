<!--
SPDX-License-Identifier: Apache-2.0
SPDX-FileCopyrightText: Copyright contributors to the Foretoken project
-->

# 服务拥有的 Profiling 生命周期

[English](profiling.md) | 简体中文

当前实验实现对已有诊断 ModelService 执行一次有时长上限的 Torch 采集，不依赖 benchmark 或监控功能。准备环境、执行命令和取得产物，请先看[操作指南](../../observability/profiling_zh.md)。

## 职责与执行路径

发起命令断线后，采集仍须停止并保留结果。因此，一次采集的身份和生命周期属于命名空间内的 `ProfileRun`，不属于命令进程，也不放进长期服务配置 `ModelService.spec`。

命令不产生请求、不修改服务配置、不部署 benchmark Job、不开放公开 profiling 端口，也不从 Pod 拷贝文件。推理 token 不等于 Kubernetes 采集权限。ProfileRun 使用 Kubernetes RBAC；内部 HTTP 沿用已有平台网络信任边界，不提供 Pod 之间的逐用户授权。

## 在采集前准备运行环境

平台通过 `profiling.artifactClaims` 将诊断命名空间绑定到已有 PVC。ModelGroup 控制器在正常 Pod 模板中添加挂载和运行身份。这会改变部署，因此应在部署诊断服务前完成；ProfileRun 本身不拥有或修改工作负载与 PVC。

Model-server 镜像内建 PyTorch 采集支持，[vLLM 补丁](../../data-plane/patches/vllm-python-profiling.patch)在原生控制入口返回启停错误，并在后续采集前重新创建已完成的 Torch 状态。Runtime 启动时准备进程身份及独占 staging 目录，收到采集请求后才启动 profiler。

## 采集身份与恢复

API 在创建后固定目标、采集工具和时长。动作从 `Capture` 推进到 `Finish` 或 `Cancel`，取消不可撤销。即使资源同名，Kubernetes UID 也能区分不同采集。

启动原生采集前，控制器先持久化删除 finalizer，并在 ProfileRun status 中保存类型明确的执行计划。它复用已有路由辅助逻辑，解析 ModelService 已提交的 serving generation，并核对 Pod → ReplicaSet → Deployment → ModelGroup 的归属链。计划记录固定的服务、Group、Pod 和 runtime 身份。控制器重启后读取原计划，不重新选择替代实例。

状态依次经过 `Starting`、`Capturing`、`Stopping`，最终为 `Succeeded`、`Failed` 或 `Cancelled`。只有全部参与实例都在采集时，运行才报告 `Capturing`；成功也需要核对全部预期参与者。发出 HTTP 操作不代表采集完成。Runtime 拒绝竞争采集，同一 UID 的重试不重启窗口，也不延长时限。

参与实例失联、被替换或服务实例集合改变时，控制器取消剩余工作，报告覆盖范围或停止确认问题。Pod 消失不等于引擎已停止；无法确认时保留 finalizer 和恢复计划供管理员诊断。删除活动运行也会请求取消。已封存产物不以 ProfileRun 为 owner，不随运行记录被垃圾回收。

## Runtime 时限

状态更新只短暂持锁。原生 utility 在 supervisor 拥有的任务中运行，不占用该锁，也不依赖 HTTP 请求任务存活。取消只改变期望动作，不丢弃正在执行的 utility。

调用者显式指定采集时长；原生启动成功后，runtime 才开始计时。原生启动和停止/flush 分别使用 30 秒、120 秒预算。提前 `Finish` 可以结束较短窗口。CLI 默认等待 10 分钟，这个观察期限不会改变 runtime 的时限。

没有活动采集时，runtime 保持正常的请求排空和退出顺序。原生 utility 失败或超时后，profiler 状态可能不确定。诊断 runtime 先关闭新请求准入，再沿已有引擎进程组关闭流程确认退出，之后释放 utility 任务。这可能中断该诊断服务的推理请求。不能确认终止时报告问题，不声称采集成功；部分输出保留供存储管理者排查。

## 先封存，再发布结果

Runtime 使用平台提供的独立、共享可写产物 PVC，不复用模型缓存或 KV 存储：

```text
<artifact-volume>/.staging/<runtime-id>/
<artifact-volume>/runs/<run-uid>/<runtime-id>/
```

开始前要求 staging 为空，不删除尚未处理的残留输出。原生停止/flush 完成后，成功采集必须为每个预期 worker 提供一份合法 Torch trace。GPU 活动单独报告，合法的空闲窗口不导致发布失败。逐事件读取避免将整个大文件加载进内存。取消时保留已有输出，但不将其视为完整采集。

Supervisor 写入并 flush manifest，在同一文件系统内重命名整个 staging 目录，再 flush 目标目录，随后发布结果引用。原子性属于单个参与者，不是分布式事务。后续采集重建 staging，使用不同 run 目录。封存失败不发布成功，也不删除可恢复数据。

Manifest 的 `startedAtUnixMs` 在原生启动后记录，`recordingEndedAtUnixMs` 表示发起停止，`exportedAtUnixMs` 表示停止/flush 返回。不同 worker 的实际停止时间可能略有差异，这些控制时间戳不冒充精确 GPU 事件边界。ProfileRun 的 `finishedAt` 是控制器观察到完成的时间。命令返回 PVC 和路径，不代表已下载到本机。删除命名空间或 PVC 仍由存储管理者负责，也可能删除产物。

## 常用命令规划

下列用法随分阶段交付逐步接入；当前采集命令见[操作指南](../../observability/profiling_zh.md)。三个入口复用同名参数。

```bash
# 部署就绪后延迟采集
foretoken deploy examples/quickstart --profile \
  --profile-delay 30s --profile-duration 15s --profile-engine pytorch

# 压测期间采集
foretoken bench examples/quickstart --dataset random --profile \
  --profile-duration 15s --profile-engine pytorch

# 真实流量下抽样记录请求链路
foretoken profile examples/quickstart --profile-duration 30s \
  --profile-request-sampling 0.01 --profile-request-limit 100

# NVIDIA Nsight Systems
foretoken profile examples/quickstart --profile-duration 5s --profile-engine nsight

# 沐曦 mcTracer
foretoken profile examples/quickstart --profile-duration 5s --profile-engine mctracer
```

## 分阶段交付

每一步是可独立使用和验证的 PR，不按 CLI、控制面、数据面横向拆分：

1. 已有服务的单次 PyTorch 采集：当前实现，显式使用 `--profile-engine pytorch` 和 `--profile-duration`。
2. 服务端延迟启动：增加 `--profile-delay`，覆盖等待期间取消。
3. 部署后采集：`deploy --profile` 在服务就绪后复用采集客户端，结束后不删除服务。
4. 压测采集：`bench --profile` 接入已有执行器的真实发送事件，不另起负载生成器。
5. 请求路径观察：与有界请求采样、服务级请求上限一起交付。只有此后省略 `--profile-engine` 才表示仅采请求链路。
6. NVIDIA Nsight Systems：增加 `--profile-engine nsight`，验证服务持续运行时完成报告导出。
7. 沐曦 mcTracer：增加 `--profile-engine mctracer`，根据实际工具验证非交互启停和导出。

所有入口使用同名的 `--profile-*` 参数，随对应能力交付。引擎 step、重复窗口及间隔参数暂缓。匹配的模型镜像提供采集工具，部署负责挂载产物存储，采集请求负责选择记录窗口。

验收需使用真实引擎产物，覆盖多次独立采集、空闲窗口、取消、CLI 退出、控制器恢复、原生失败和产物发布失败。多 worker 覆盖和开销必须有各自的硬件证据，单 worker 成功不代表这些验证已经完成。

## 上游参考

- [vLLM profiling](https://docs.vllm.ai/en/stable/contributing/profiling/)：引擎配置、输出和诊断开销。
- [PyTorch profiler](https://docs.pytorch.org/docs/stable/profiler.html)：原生采集与 trace 导出。
- [Dynamo Profiler](https://docs.dynamo.nvidia.com/dynamo/dev/knowledge-base/modular-components/profiler/overview)：部署性能标定与有限 runtime trace 采集的产物和职责不同。
