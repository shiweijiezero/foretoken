<!--
SPDX-License-Identifier: Apache-2.0
SPDX-FileCopyrightText: Copyright contributors to the Foretoken project
-->

# Request 级 Draft/Target 路由

[English](frontend-workflow.md) | 简体中文

Frontend 规范化输入、sampling 和 stop，沿用现有请求 admission reservation。
Router 通过现有 Filter → Scorer → Picker 为每个 request 选择一个 Target 和一个兼容
Draft，绑定持续到请求结束。Target 推进轮次，各引擎原生 Scheduler 决定 token budget、
KV 分配、preemption 和实际物理 batch。无需独立共享 DT scheduler 进程或 Deployment。

## 运行 token 输入诊断

按 [README](../README_zh.md) 启动角色，在仓库根目录构建：

```bash
make vllm-source
cd data-plane
cargo build --locked -p foretoken-server --examples
```

将 `DRAFT_URL`、`TARGET_URL` 设为角色地址。`request.json` 的最小内容：

```json
{"request_id":"example","prompt_token_ids":[1,2,3],"sampling_params":{"temperature":0,"max_tokens":24}}
```

```bash
./target/debug/examples/draft_target "$DRAFT_URL" "$TARGET_URL" < request.json
```

使用共同 tokenizer 的真实 IDs。这个底层接口不推断 EOS；`_eos_token_id`、
`stop_token_ids`、`_all_stop_token_ids` 由模型感知的规范化提供。调用者管理 deadline。
地址必须同时可被 Frontend 和 Target 角色访问，因为 Target 直接调用 Draft。
使用实例地址；会切换实例的负载均衡地址无法维持这些 engine sessions。

并发请求的 `batch.json` 示例：

```json
{
  "model": "example-model",
  "workers": [
    {"endpoint":"http://draft-host:19100","scope":"dt"},
    {"endpoint":"http://target-host:19100","scope":"dt"}
  ],
  "requests": [
    {"request_id":"a","prompt_token_ids":[1,2,3],"sampling_params":{"temperature":0,"max_tokens":8}},
    {"request_id":"b","prompt_token_ids":[1,2,4],"sampling_params":{"temperature":0,"max_tokens":24}}
  ]
}
```

```bash
./target/debug/examples/draft_target_batch < batch.json
```

按固定 N:M 增加 worker 条目。Router 为每个请求选一对；兼容 Draft 可服务绑定到不同
Target 的请求。标准输出是普通 `TokenOutput` NDJSON，标准错误记录选中的端点。
Ctrl-C 关闭 stream；未收到 terminal output 就 EOF 属于失败。

## 执行归属

```text
Frontend → Router：一次选择 [Draft] + Target，保留 routing reservations
         → Target RoleService.generate → TargetCoordinator（每 request 一个）
             ├─ 在原始 prompt frontier 打开指定 Draft control session
             ├─ 原生 Target prefill → confirmed delta + ticket
             ├─ 将准确的 confirmed delta commit 给 Draft
             ├─ Draft propose → 原生 Draft generation → IDs / handle
             └─ 本地 verify：准备一个 proposal → 原生 ticket admission
Frontend ← confirmed output ← 原生 Target sampling 与 stop 处理
```

`TargetCoordinator` 管理累计确认输出 frontier、round 和固定绑定，转发准确的确认
delta。原生 Target state 管理完整 token 前缀；`DraftSession` 保留同步前缀，用于每轮
普通 Draft 请求。
`RoleService` 管理原生 session、当前 ticket、已启动操作和清理。原生 ticket 由内部
request ID 和确认输出计数组成，admission 只消费一次；候选不能推进确认前缀。
Target 按配置的默认候选数（3）和剩余输出长度选择本轮 K，
引擎容量单独配置，见[候选数量契约](role-protocol.md#candidate-policy-and-capacity)。没有额外跨 request 阶段 policy、
waiting window 或 logical batch；原生 Scheduler 将独立 ready 的请求组批。

慢 Draft 或传输只挂起自己的 request coroutine。其他请求可以先 admission 并进入
同一个物理 batch。实际 batch 记录 ID、wall-clock 区间、scheduled tokens，以及成员的
engine request ID、客户端 request ID、confirmed frontier 和
`{request_id, work_id, frontier, round, phase}`。HTTP 调用次数不能证明 GPU batch；
schedule-to-result 是 wall time，不是 GPU event 测量。

Foretoken Router 管 request placement 和路由负载。DT plugin 接入原生 Scheduler、
Worker、ModelRunner，复用 KV、batching、forward 和 sampling。维护中的 vLLM patches
提供需要的底层 hooks 和平台修复。Prefill 使用正常 Target 路径，计入原生 workload。
插件 entry point 也必须在 engine 子进程加载；版本边界见
[MRV2 integration contract](mrv2-integration.md)。

## 策略开发位置

Request placement 复用现有 Router 的 Filter → Scorer → Picker。
`RouteCandidate` 提供兼容能力、当前观测和路由 reservation；`RouteSession`
按请求生命周期持有并释放选定的一对实例。

`TargetCoordinator.select_candidate_count()` 是由 `advance()` 实际调用的轮次
budget 扩展点。默认读取启动器拥有的 K 配置，省略时为 3，并受剩余输出长度、
绑定 Draft 的方法上限和 Target 原生容量约束。Coordinator 继续拥有 frontier、ticket、
提交和清理；K 选择方法不提交工作，也不修改这些执行事实。
配置默认值与启动时容量保持独立，没有策略注册框架或动态 K 算法。

本地 batching 属于各 engine 的 Scheduler。`ObservedScheduler.schedule()`
观察原生决策；`ExternalScheduler.schedule()` 排除仍等待远端输入的请求，再调用
原生调度。以后修改 batch 成员、优先级或执行安排，应在这个 Scheduler 边界实现，
保留原生 token/KV accounting 和 preemption。Worker/Runner 与 Connector
继续准备、执行和回收选中的工作。

轮次反馈包括实际 requested/proposed/verified/accepted/committed token 数、
可空的 engine ticket `admitted`、proposal 传输，以及 Target→Draft features 的
`context_transfer`。request、round、work、frontier 和 worker 地址关联原生
physical batch。Frontend 将它们记录为 trace 和有界 metrics，覆盖两个传输方向；
主机侧 wall time 不作为 GPU 时间。
Role 日志将 proposal/verification 的 work identity 关联到 artifact handle 和
feature 来源。即使取消使最后一轮没有输出，也记录实际完成的读取及 producer ACK。

## 托管服务 profiling

DT model-server 复用普通服务的 profiling supervisor、ProfileRun API、超时和
artifact 发布流程。私有 role HTTP 只将控制命令接到原生 engine utility。
沐曦使用已有 mcTracer Worker mixin，包住保留平台行为的 DT Worker，不复制模型
执行方法。设置 `ModelService.spec.profiling.engine: mctracer`，沿用
[`foretoken perf/profile` 流程](../../../benchmarks/docs/profile/README.md)。
完成状态不确定的 capture 持续由 supervisor 管理，直到 engine 进程退出。
性能数值应在不采集 profiler 的独立运行中测量。

## 取消、drain 与恢复边界

Frontend 关闭 stream 时显式关闭 Target session。Target 关闭对应 Draft control
session、abort 原生工作并等待已启动操作。HTTP 调用取消不会取消传输所有者的
read/ACK；buffer 回收需要 compute fence 与消费者 ACK。失败的 cleanup 保持失败，
不能将其 session state 报为 `closed`。

Drain 拒绝新绑定，允许已有轮次结束。发现配置更新时，活跃 stream 保留原先 client。
多个 Frontend 副本独立路由新请求，每个已接纳请求只有一个 Target 轮次 owner。
Peer 或进程失败终止该请求，不做透明 replay。

生产路径当前为固定单 Draft/Target、每轮一条候选链。token-only 只支持 greedy；tensor adapter 也支持原生随机采样。端点列表为
未来多 Draft 保留边界，现在拒绝多个条目。现有 RDMA/neural 操作和手动
checkpoint/resume 保留 adapter 的模型、sampling 和布局限制；尚无自动迁移或新
placement policy。详见 [role protocol](role-protocol.md) 和
[connector contract](connector-contract_zh.md)。
