<!--
SPDX-License-Identifier: Apache-2.0
SPDX-FileCopyrightText: Copyright contributors to the Foretoken project
-->

# Profiling 执行架构

[English](profiling.md) | 简体中文

**状态：待评审的架构设计，接口尚未实现。** 本设计替代由工作站控制的 profiling 原型；监控和请求 tracing 继续作为独立功能。

## 用途与用户路径

Profiling 用于在明确的请求负载下分析推理引擎的时间开销，不是普通性能评分。负载生成器不应同时成为 Pod 控制器和文件传输服务。

拟定入口保持简单，针对已经部署的诊断服务：

```bash
foretoken deploy examples/quickstart
foretoken bench examples/quickstart --profile --profile-duration 15s
```

平台先为工作负载命名空间启用受支持的 profiler，并准备持久产物存储。配置在服务启动前生效，采集过程中不临时改写 Pod。

首版面向一个能够明确选定、已经 Ready 的 ModelService，使用经过验证的 vLLM Torch adapter。Profiling 不隐式部署或删除模型服务，普通 benchmark 的部署行为不变。仅提供公共 URL 无法确认有权控制的采集对象，因此首版不支持 URL-only profiling。

## 不改变 benchmark 执行位置

当前 benchmark 及其请求执行重构均在本机运行，没有 Kubernetes benchmark Job。Runner 只接收公开推理 URL、模型和请求配置；重构后的标准路径由 EvalScope 独占 client、事件循环、调度和取消。

Profiling 只在**一次**标准 benchmark 执行外增加轻量编排：

1. 解析已有 ModelService 和公开入口，在采集前完成配置、数据集物化、tokenizer 输入及 adapter 导入准备；准备仍归已有执行 adapter，不额外启动 client 或另一场 benchmark。
2. 创建 `ProfileRun`，等待控制面确认进入 `Capturing`。如果等待期间已经进入终态，直接结束本次尝试，不再启动无关负载。
3. 通过公开 URL 执行一次标准 benchmark。
4. 负载结束时请求 `Finish`，中断时请求 `Cancel`。
5. 读取采集终态，输出持久产物引用。

实施时需要在现有 adapter 内明确准备和执行两个阶段，不假定 EvalScope 已有该接口。同时记录执行器实际发请求的区间与 runtime 的采集区间。如果无法前置的执行器启动工作耗尽了窗口，实际负载没有重叠，则 benchmark profiling 结果为 `NoWorkloadOverlap`，即使已有合法文件也不能声称采集到了目标负载。ProfileRun 成功仅表示采集完成，不表示 benchmark 成功。

`duration` 表示最大墙钟采集窗口。提前 `Finish` 会封存较短窗口；到达时长后，即使 benchmark 还在运行，采集也会停止。结果记录实际起止时间，不声称整场 benchmark 的请求都落在采集窗口中，也不进入普通 W&B/Pareto 性能比较。

首版不保留原型的 `--warmup-requests`。现有 EvalScope adapter 没有可直接使用的阶段钩子，无法保证精确的预热切换。不能用两次 benchmark、另一个预热 client、复制调度器或 monkeypatch 替代。可以先执行一次普通 benchmark 预热引擎，但不能把它称作同一 client 的连续稳态预热。后续需要精准切窗时，应使用执行器自身的正式扩展点，并区分 conversation 与 HTTP turn 的计量单位。

## 独立的采集生命周期

```text
本机 benchmark ── 公开 URL ──> Frontend ──> model-server
        │
        └── Kubernetes API ──> ProfileRun
                                  │
                            现有控制面 manager
                                  │ 内部控制
                                  v
                          model-server supervisor
                                  │
                          vLLM / Torch profiler
                                  │
                            持久产物 PVC
```

| 职责 | 唯一 owner |
| --- | --- |
| 请求、并发、速率、多轮对话 | 现有 benchmark 执行器 |
| 提交、结束、取消和观察采集 | CLI profiling 编排适配器 |
| 解析参与实例、协调采集、发布状态 | 现有控制面中的 ProfileRun reconciler |
| 引擎独占采集、原生 utility 和失败升级 | model-server supervisor 与薄后端 adapter |
| 存储和保留产物 | 平台提供的持久存储 |

不新增 benchmark Job、Controller Deployment、公开 profiling Gateway 路由、隧道、`kubectl exec/cp` 回收链路或通用 profiler 插件框架。

### 为什么新增 ProfileRun

一次采集有自己的身份、动作、超时、失败恢复和结果保留周期。它必须在 CLI 断线后继续存在，并能在控制器重启后恢复，不属于 ModelService 的长期服务配置。

因此新增一个 namespaced `ProfileRun` 及其 reconciler，比把临时命令塞入 `ModelService.spec` 更清楚；后者还可能使服务 generation 失效。用 Job 加命令/状态 ConfigMap 模拟同一套行为，本质上仍是在另造控制协议。

请求示意：

```yaml
apiVersion: inference.foretoken.io/v1alpha1
kind: ProfileRun
metadata:
  name: qwen-diagnostic
  namespace: foretoken-demo
spec:
  modelServiceRef:
    name: quickstart-qwen3-0.6b
  duration: 15s
  action: Capture
```

使用 Kubernetes UID 作为采集身份；同名资源重新创建后是另一次运行。`action` 从 `Capture` 推进到 `Finish` 或 `Cancel`，采集开始后不再修改目标和时长。这里需要的是执行身份，不是内容 hash。

Status 仅发布阶段、observed generation、实际时间、参与者数量、简明失败原因和产物引用，不暴露 Pod 地址或 rank 清单。执行计划存入由 ProfileRun 拥有的 ConfigMap。在发送任何 start 操作之前，控制器必须先持久化取消 finalizer 和执行计划；计划包含 ModelService UID、已提交的 serving generation 及选定的 Group/runtime 身份。重试只读取同一计划，不重新选择替代实例；API 写入得到确认前不得启动引擎采集，保证每个已启动实例都有恢复记录。参与者从选定 ModelService 已提交的 serving generation 解析，不复制路由策略；Pod 和内部 endpoint 不成为 benchmark 的用户目标或参数。

## 启停与恢复

控制面推进 `Pending → Starting → Capturing → Stopping`，最后进入 `Succeeded`、`Failed` 或 `Cancelled`。发出 HTTP 请求不代表采集成功，状态必须跟随实际运行结果。

Engine adapter 必须区分启动成功、停止并 flush 完成、原生执行失败。Pinned vLLM wrapper 目前会吞掉部分启停异常，只写日志；支持该 runtime 需要一个窄而可复用的上游错误传播扩展，不能用 ACK 加文件存在检查代替。在满足这个契约之前，adapter 应明确报告不支持 profiling，而不是承诺可靠采集。

每个引擎实例只接受一个活动采集。同一 run UID 的重复操作返回已有状态，不重新启动、不延长截止时间；另一个 run 得到 `Busy`，不暗中排队或替换。

状态转换只持有短锁。原生 engine utility 交给 supervisor 拥有的操作任务，在锁外执行，不随 HTTP 请求被丢弃。Supervisor 在 utility 未返回时仍能处理取消、截止时间和进程终止。

采集时长、启动操作超时和停止/flush 超时是不同预算，默认值只在 profiling runtime 配置中定义一次。不得再用“请求数乘 benchmark HTTP 超时”估算 profiler 生命周期。

停止超时表示引擎状态未知。对于明确启用了 profiling 的诊断实例，supervisor 关闭 admission，直接进入现有进程组终止流程，不先等待已卡住的 utility 或其锁。确认进程退出后，才能把部分产物视作不再被写入。这可能中断该实例上的请求，因此 profiling 不是常开的、无侵入生产能力。如果无法确认退出，必须保留明确失败状态，而不是声称已经停止或承诺操作系统层面无条件成立的返回时限。

控制器重启后读取执行计划，查询相同的 runtime 身份。实例被替换或服务拓扑发生变化时，记录覆盖不完整，不把采集转移到新实例，也不修改服务的扩缩容策略。删除活动 ProfileRun 应触发取消，并禁止新的采集启动。Finalizer 在确认停止或进程退出前保留执行计划；停止无法确认时保留可诊断状态，不让 GC 提前删掉恢复信息。同一 run UID 的取消优先于迟到的重试。移除 finalizer 和计划，不删除产物 PVC 或已经封存的 manifest。

## 先持久化，再发布完成

Model-server 直接写入平台预先提供的、命名空间内的独立 artifact PVC。它不是模型下载缓存、`emptyDir` 或本机 benchmark 输出目录。所有参与 runtime 都必须能写入该存储，模型 Pod 替换后数据仍保留；PVC 不以 ModelService 或 ProfileRun 为 owner。

每个 runtime 有独占 staging 目录，每次采集有独立结果目录：

```text
<artifact-volume>/.staging/<runtime-instance>/
<artifact-volume>/runs/<run-uid>/<runtime-instance>/
```

引擎固定写 staging。开始前确认该目录没有活动采集或尚未处理的残留，不静默清除部分结果。停止后，等待全部预期 worker 返回，并由 adapter 核对所需 trace 及格式；随后在同一文件系统内将整个目录 rename 到本次 run 目录，发布小型 manifest。下一次采集重新创建空 staging。

这样可以完整保留子目录和固定文件名的报告，不再依赖旧文件集合差分、prefix 猜测、hash 或逐文件搬运。Rename 只保证单个参与者目录原子封存，不是整个分布式 run 的事务。只有全部预期参与者完成封存，控制器才能发布成功。重复结束操作返回已有 manifest，不覆盖历史目录。启动期 CUDA graph profiling 不得向正式采集的 staging 写入文件。

结果引用包含 PVC、run 目录和 manifest。本机 `--output-dir` 可以保存运行摘要和引用，但不能声称已经下载了 Torch trace。用户通过平台已有存储入口访问文件；首版不增加自动下载或新的产物 HTTP 服务。

删除 Namespace 仍可能删除其中的存储。因此首版要求已经存在的部署，不进入 benchmark 的隐式创建/删除流程。Namespace/PVC 的主动删除由存储 owner 负责，不承诺越过该边界的数据保留。

## 失败语义

| 事件 | 必须得到的结果 |
| --- | --- |
| CLI 退出或断线 | Runtime 自己按时长停止采集；运行记录和已保存结果仍可通过 Kubernetes 查询 |
| 一个参与者启动失败 | 停止已经启动的参与者，保留诊断并判定运行失败 |
| 原生启停 utility 不返回 | Supervisor 按独立预算处理，不被 session 锁挡住 |
| Engine ACK 成功但没有有效 trace | 返回 `EmptyCapture` 或具体存储错误，不判定成功 |
| Pod 或 serving generation 改变 | 报告覆盖不完整，保留已有部分结果 |
| 存储无法 flush 或封存 | 发布失败，不声称已保存，不删除可恢复的 staging |
| 用户中断负载 | 现有执行器停止调度；编排请求取消，runtime 截止时间负责兜底 |

创建和取消 ProfileRun 通过 Kubernetes RBAC 授权，与发送推理请求的权限分开。内部采集控制不经公开 Frontend/Gateway 暴露。NetworkPolicy 沿用平台信任边界，不把标签选择器描述为共享命名空间内的逐用户鉴权。

## 实施与验收

独立 profiling PR 应从上述生命周期实现，不恢复旧的本机隧道类、Pod 拷贝恢复、请求调度器或文件集合记账。恢复用户 `--profile` 入口前，先对齐标准 benchmark 执行器重构。通用接口只表达采集意图和结果，vLLM utility 与 Torch 格式细节留在 adapter。

真实诊断部署验收应覆盖：正常采集且 trace 可打开、第二次采集不混入旧文件、中断后的结果保留、控制器重启、utility 卡住、存储封存失败。正常完成后推理仍可用；失败案例要确认 supervisor 的实际处置。使用现有验证流程和直接实验，不新增永久 CI 矩阵或合并门禁。

此前 A100 原型只证明了单机采集成功，以及短控制超时后的手工恢复，不是本设计的验收证据。此文档不修改 CRD 或生产代码。

## 上游依据

- [vLLM profiling](https://docs.vllm.ai/en/latest/contributing/profiling.html)：原生 profiler 配置与诊断用途。
- [Pinned vLLM worker](https://github.com/vllm-project/vllm/blob/1be36283678a9a94fc8fdaad6c95c2896d6b4015/vllm/v1/worker/gpu_worker.py)：Torch wrapper 可能跨采集保留第一次的 worker name。
- [Pinned profiler wrapper](https://github.com/vllm-project/vllm/blob/1be36283678a9a94fc8fdaad6c95c2896d6b4015/vllm/profiler/wrapper.py)：启停异常可能只写日志，不能只凭 utility ACK 判定完成。
- [Pinned EngineCore client](https://github.com/vllm-project/vllm/blob/1be36283678a9a94fc8fdaad6c95c2896d6b4015/rust/src/engine-core-client/src/client.rs)：profile utility 等待 engine 回复，没有操作 deadline。
- [PyTorch profiler](https://docs.pytorch.org/docs/2.14/profiler.html)：复用采集和导出机制；schedule 的步数不是墙钟时间，也不是 benchmark conversation 数。
- [Kubernetes Job](https://kubernetes.io/docs/concepts/workloads/controllers/job/)：结束负载 Job 不会停止另一个 workload 中的 profiler；保持当前 benchmark 边界不需要新 Job。
- [Dynamo Profiler](https://github.com/ai-dynamo/dynamo/blob/main/docs/fern/pages/developer-guide/knowledge-base/modular-components/profiler/overview.md)：部署性能标定不同于本设计的一段运行时 trace 采集。
