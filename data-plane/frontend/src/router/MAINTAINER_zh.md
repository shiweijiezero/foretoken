<!-- SPDX-License-Identifier: Apache-2.0 -->
<!-- SPDX-FileCopyrightText: Copyright contributors to the Foretoken project -->

# Router 维护指南

[English](MAINTAINER.md) | 中文

Router 算法编译在 Frontend 二进制中，不是运行时插件，也不是公共扩展接口。

## Pipeline 契约

每个请求依次经过：

```text
兼容且健康的候选项
→ Filter 返回的下标
→ Scorer 返回与保留候选项一一对应的分数
→ Picker 返回的下标
→ RouteDecision
```

- `RouteFilter` 返回需要保留的候选项下标。
- `RouteScorer` 按相同顺序为每个保留候选项返回一个 `RouteScore`。
- `RoutePicker` 返回 scored candidates 中的一个下标。

Router 负责候选项身份，并校验重复或越界的下标以及分数数量不一致。算法不能维护第二份路由目录，也不能在请求路径查询 model-server；算法接收的是当前选择轮次中不可变的观测快照。需要共享 KV 命中的 Filter 或 Scorer 应让 `needs_kv_prefix` 返回 `true`，由 `Router::start` 先通过 KV indexer 异步准备观测，再执行同步 pipeline。

## 添加算法

在 `src/algorithm/filter/`、`src/algorithm/scorer/` 或 `src/algorithm/picker/` 下实现对应接口，然后在对应 stage 的 `mod.rs` 中为 `declare_router_algorithms!` 列表增加一项，填写模块名、类型名和用户配置名称。该宏会生成模块声明、公开导出和编译期 descriptor 注册；不需要修改 Controller enum 或 CRD。只有可观察行为发生变化时，才同步维护中的示例、面向用户的 Router README 和 contract tests。

请求级共享状态放在 `RouterPipeline::with_customized_context` 中。Router 为每个请求创建一个 context，并在请求结束后释放。联合 E/P/D 实现可以在初始轮读取完整候选快照，把计划中的各阶段目标保存在 context 中，再由 Picker 在每个阶段返回对应候选项。

## 多阶段路由

算法对完整的兼容、健康候选项快照进行评分。Picker 执行前，Router 会将候选项限制到当前执行阶段和已选择的控制器定义 connector compatibility scope。scope 可以包含多个 Encoder、Prefill 和 Decode ModelGroup；它保护真实传输契约，但不按 ordinal 固定配对。Picker 仍然每阶段选择一个候选项，请求内 context 可跨轮保存联合计划。

## 打分契约

`least_loaded` 和 `kv_least_loaded` 共用负载计算，取 Model Server 活跃请求数、调度器运行与等待
请求数之和、frontend 在候选项精确 DP rank 上跟踪的活跃请求数三者的最大值。
这些计数有重叠，不能相加；缺失遥测时仍可使用本地计数。端点遥测由各 rank 共享，不能提供
引擎的逐 rank 负载。Prefill 对同一 pipeline scope 内下游 Decode 的比较也使用此计算。

各 scorer 使用以下观测和公式：

| Scorer | 输入 | 分数 |
| --- | --- | --- |
| `queue_depth` | `scheduler_waiting_requests` | `(max - waiting) / (max - min)` |
| `running_request` | `scheduler_running_requests` | `(max - running) / (max - min)` |
| `kv_cache_utilization` | `kv_cache_usage` | `1 - usage` |
| `active_request` | 本地活跃请求数 `count`、候选项最大值 `maxCount` | `count <= idleThreshold` 时为 `1`，否则为 `(maxCount - count) / maxCount * maxBusyScore` |

`queue_depth` 和 `running_request` 使用传入 `score` 的全部候选项求最小值和最大值；计数全部相等时得 `1`，空候选集返回空分数列表。
计数先相减再转为 `f64`，避免大整数提前转换丢失差值。
数值通过 `RouteScore.preference` 原样传给 Picker，其余位置和负载字段为零。
原有位置策略继续使用字典序。

Registry 负责指标历史：立即发布 gauge，并仅在原始观测快照仍处于保留窗口内时为缺失项使用之前的实测值。
时间戳未递增、累计计数器或直方图发生重置时清空历史。新历史中缺失的 gauge 保持未观测状态，
直到生产者重新上报；指标打分器将未观测值映射为零。
速率和窗口延迟统计在计数器窗口足够前保持不可用。

Foretoken 负责遥测传输、健康检查、DP 展开及 E/P/D 阶段资格判断。
Model Server 端点报告各引擎 scheduler 计数之和及 KV 使用率均值。
同一端点的所有 rank 得到相同分数。这三个指标 scorer 不使用 `RoutingProgress`，
Router 仍传入该参数并负责后续阶段选择。

`active_request` 使用传入 `score` 的全部候选项求 `maxCount`。`idleThreshold` 默认 `0`，
负值归零。`maxBusyScore` 默认 `1`，范围 `[0, 1]`；缺失、null 或超出范围时使用 `1`。
每个已选择阶段持续计数，直到阶段完成或 session 释放。

`active_request` 使用 frontend 按目标和 DP rank 维护的本地预留量，不叠加引擎调度指标或其他 frontend 的请求。
选择目标和预留共用一把锁；路由 session 负责清理，RuntimeBuilder 在 serving snapshot 替换时保留此状态。

可选参数配置在 `FrontendService.spec.routerPipeline.scorerParameters` 中，由所选 scorer 在 frontend 启动时读取。
