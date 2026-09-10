<!--
SPDX-License-Identifier: Apache-2.0
SPDX-FileCopyrightText: Copyright contributors to the Foretoken project
-->

# 按需 Profiling 设计

[English](profiling.md) | 简体中文

**状态：目标设计，已实现本地单窗口原型。** 本地已有 `bench --profile`、延迟、时长、运行时取消/查询及 exec 收取结果，尚未完成 GPU 集成验证。下文的独立 `profile`、重复/间隔、样本上限和其他后端仍是方案，不是已发布接口。当前命令见 [benchmark 指南](../../benchmarks/README_zh.md#按需-profiling)。

## 1. 两个入口，一套采样能力

Profiling 用于记录模型处理请求时的内部执行。它是一条有限时长的一次性命令，不随模型部署持续采集，也不依赖 Prometheus、Grafana 或告警是否启用。

- `foretoken bench PATH --profile`：benchmark 提供请求，同时启动短窗口采样。请求内容、并发和速率仍由已有 benchmark 执行器负责。
- `foretoken profile`：选择已经运行的 Foretoken 服务，对其现有流量采样，不隐式发送额外请求。服务选择与授权的最终语法见第 6 节。

两者提交同一种采样计划。用户不修改 example、Chart 或 `observability` YAML，不手动查 Pod、转发端口或执行 start/stop HTTP 请求。服务镜像和运行时负责准备 profiler；具备采样能力不等于始终在采样。

## 2. 用小窗口限制采集量

下面是候选参数及语义，两种入口保持一致；尚未确定的单位不能直接变成公共字段。

| 选择 | 候选参数 | 含义 |
| --- | --- | --- |
| 工具 | bench 的 `--profile`；独立命令的工具选项待定 | Torch、Nsight Systems、沐曦原生工具；只开放已经验证的实现 |
| 初始延迟 | `--profile-delay` | 计划开始后，等待多少秒才开始第一个窗口；不是模型部署后的时间 |
| 窗口长度 | `--profile-duration` | 每个窗口的目标采集秒数，到期请求停止；文件导出时间另计 |
| 窗口数量 | `--profile-repeat` | 总共执行几个窗口，包含第一个；必须是有限次 |
| 窗口间隔 | `--profile-interval` | 上一个窗口停止并完成导出后，到下一个窗口启动前的等待时间，不是固定频率 |
| 每窗口样本上限 | 名称与单位待确认 | 达到上限或时长到期即停止该窗口；不能混同请求数、引擎步骤和工具采样点 |

例如：等待 30 秒，采集 5 秒，完成导出后等 20 秒，再采下一窗，共 3 窗。这些数字只是说明，不是默认值。默认应为一次短采样；具体默认时长在真实小负载验证后确定，不提供无限持续采样的默认行为。

没有请求时，窗口仍按时间结束，并说明没有捕捉到推理工作；不自行延长或制造流量。采样开始和结束的实际时间随结果报告。多个实例独立执行，不承诺跨 Pod 精确同时开始。

vLLM 的 `delay_iterations` / `max_iterations` 和 [PyTorch schedule](https://docs.pytorch.org/docs/2.14/profiler.html#torch.profiler.profiler.schedule) 以执行步骤计数，不能直接代替秒数。一个引擎步骤也不等于一个用户请求。若 sample 最终指请求数，还需要定义请求开始/完成的计数点，以及批处理、prefill/decode 下的含义，不能用 `bench --number` 冒充精确的服务端请求筛选。

## 3. 本机提交，集群内计时和执行

本机命令只负责选择目标、提交完整计划和取得结果。真正控制采样窗口的代码留在目标 model-server 的进程管理路径里，不依赖本机不断发送定时 start/stop。

```text
bench --profile ─┐
                ├─ 1. 共享提交代码 ─ 2. Kubernetes 管理通道
profile 服务 ───┘                         │
                                         ▼
                        3. model-server：等待 → 采集 → 导出 → 间隔
                                         │                └─ 有限次重复
                                         ▼
                        4. 后端 adapter → 引擎内 profiler
                                         │
                                         ▼
                        5. 保存各窗口结果 → 命令自动收取并报告
```

本地单窗口实现复用 Kubernetes 的 Pod exec 通道，在目标容器内调用其已有 model-server 管理端口，将窗口交给进程内任务。exec 只提交和查询，不承载计时循环；用户无需运行这些内部命令。这样不新增 TCP 监听端口、Service 或 Gateway 路由，也不使用工作站 port-forward。

目标发现复用服务的当前 serving generation，操作范围限于选定 Foretoken 服务。Kubernetes RBAC 保护 exec 调用；管理处理器另外要求连接来自 loopback，不信任转发地址请求头。CPU 检查已覆盖这些处理器，但实际部署的监听配置和 Kubernetes 权限仍需端到端验证。

model-server 持有活动采样任务，控制请求返回后任务仍执行。窗口到期请求停止，导出结束后才进入下一窗；取消会停止后续窗口并请求结束当前采样。同一引擎已有采样时明确拒绝重叠，不悄悄替换。部分实例启动失败时，取消本次已启动实例并报告部分结果。

本机失联不应使已提交窗口无限运行。原生 stop/导出失败或卡住时，需要报告失败或停止尚未确认，不能把计时器到期当成已经停止；不为一次诊断默认杀死或重启已有服务。Pod 重启不自动恢复旧计划，也不转移到替代实例。这里不引入新的 CRD、reconciler、Job 或常驻采样服务。

benchmark 仍执行一次正常的请求流程：完成输入准备，在将要发送请求时启动采样计划；负载结束或用户中断时结束剩余采样，不为凑满窗口重复运行 benchmark。延迟阶段也可以有请求，不能等到采样结束才开始发请求。实现需对齐已有执行器的准备/执行边界，不另建客户端、请求调度器或预热请求路径。

结果按命令、窗口和实例区分，adapter 保留后端原生文件，避免连续采样覆盖。共享提交代码在文件完成写入后自动收取，不要求用户 `kubectl cp`，也不新增下载端口或要求专用 PVC。未收取的文件按现有 Pod 存储生命周期保留，不承诺 Pod 删除后仍存在。benchmark 拥有的临时服务必须在采样停止、结果处理后再清理；收取失败要保留可恢复结果并报告，不静默删掉。

## 4. 统一命令，不假定后端启动方式相同

| 后端方向 | 需要实现或验证的边界 |
| --- | --- |
| PyTorch Profiler | 复用引擎启停与导出接口；NVIDIA 和沐曦适配版 PyTorch 分别验证实际设备事件，不能以 CPU-only trace 冒充 GPU 支持 |
| Nsight Systems | 复用 `nsys` 和原生 capture 控制；确认已有服务是否能动态附加、如何导出多窗口结果以及运行期开销 |
| 沐曦原生工具 | 先确认具体工具、版本、启动/停止与产物接口，再接入同一份计划；不猜测工具名称或复制 NVIDIA 命令 |

[vLLM 的 Torch 与 Nsight 示例](https://docs.vllm.ai/en/latest/contributing/profiling/)说明了两者启动条件不同：官方 Nsight 服务示例先用 `nsys` 包装服务进程。用户不改 YAML 的目标，由 Foretoken 镜像和启动集成承担；不能据此声称任意已有进程都能无准备附加采样。不能在捕捉命令中隐式滚动重启已有服务。未具备所选后端能力时，在开始前明确报告。

参考 [Dynamo Runtime Profiling](https://github.com/ai-dynamo/dynamo/blob/main/docs/fern/pages/developer-guide/knowledge-base/modular-components/profiler/profiler-guide.md#runtime-profiling) 的引擎控制与后端差异处理；不把部署参数搜索、性能标定和自动部署一起带入短窗口采样。

## 5. 代码责任和实现顺序

| 所在位置 | 此次功能的责任 |
| --- | --- |
| [`cli/foretoken/`](../../cli/foretoken/) | 两个入口共用目标选择、计划提交、状态观察和结果收取；复用现有 Kubernetes 工具 |
| [`benchmarks/`](../../benchmarks/) | 提供请求，在一次执行中接入采样计划；不拥有采样计时或 Pod 生命周期的第二套实现 |
| [`data-plane/model-server/`](../../data-plane/model-server/) | 进程内计划执行、启停、取消和后端适配；复用现有进程管理和推理引擎 client |
| [`deploy/`](../../deploy/) | 镜像中的工具及必要运行时准备；不新增用户 profiling YAML 开关 |
| [`data-plane/third_party/vllm/`](../../data-plane/third_party/vllm/) | 仅保留可上游复用的底层修复，不放 Foretoken 的命令、计时或 Kubernetes 逻辑 |

先用真实负载验证已实现的延迟 Torch 窗口，再让独立入口复用同一控制路径，补齐间隔、有限次重复及定义明确的样本限制；分别验证 Nsight 和沐曦的启动条件后接入。各阶段均用真实小负载验证窗口、导出、连续运行、取消和普通推理；不新增永久测试或 CI 作为前置工程。

本地原型已用运行时单窗口替换采样端口转发、整场 benchmark 采样、额外预热请求和文件集合差分。已有监听端口上的 `PUT/GET/DELETE /v1/internal/profile/{id}` 分别提交、查询和取消客户端生成的 ID，这些处理器只接受 loopback 连接。监督器在内存中保留活动或最近一次结果；新采样替换结果或进程重启后，旧 ID 会明确报告不可用，不转向另一项采样。原生文件仍遵循 Pod 存储生命周期。原生控制失败后不再接受新采样，等待运行时恢复，但不会为此重启服务。命令收取失败时保留 benchmark 创建的资源。model-server Dockerfile 会安装仓库维护的 Python 补丁，提供逐次命名和导出错误传播，不依赖 Rust 构建所用 vLLM 子模块里的未提交修改。引擎镜像要求见[源码镜像生命周期](source-image-lifecycle_zh.md)。GPU 集成仍待验证。普通推理访问方式保持不变。

## 6. 实施前需要明确的两项接口

1. **sample 的单位和范围**：请求、引擎步骤还是工具采样点？上限是每个窗口、每个 worker，还是整项服务的总量？此处未定，不先暴露模糊的 `--samples`。
2. **已有服务的身份与授权**：会议中的“服务 token”具体是什么，是否有现成的服务定位和管理授权接口？可先评审 `ModelService + namespace` 的管理入口，但不能将普通推理 API key 等同于 Pod 控制权限；也不自行实现“token 触发 prefill”的新协议。

这两项未确认不影响整理共用的时间窗口与执行责任，但会影响最终命令语法和验收标准。Nsight/沐曦的附加条件属于后端可行性验证，不应靠增加用户 YAML 掩盖。
