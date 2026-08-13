<!-- SPDX-License-Identifier: Apache-2.0 -->
<!-- SPDX-FileCopyrightText: Copyright contributors to the Foretoken project -->

# Router

Router 为每个执行阶段选择一个可路由的 ModelGroup(即engine-core-server) 和精确 DP rank。

Filter–Scorer–Picker 是候选路由项（eligible route option，candidate）列表级接口：

- **Filter** 接收兼容且健康的候选路由项快照，返回保留子集的索引。它不能新增或修改候选路由项；越界或重复索引会成为明确的路由错误。
- **Scorer** 按保留候选路由项的原有顺序为每项返回一个 `RouteScore`。Router 持有 candidate/score 视图；数量不匹配会成为明确的路由错误。内置 KV 评分比较 prompt 命中长度、存储层级、locality 和负载。
- **Picker** 从当前评分列表选择一个索引，而不是回传候选路由项。非空列表返回 `None` 或越界索引都会成为明确的路由错误。Router 随后输出 `RouteDecision`，其中包含 ModelGroup `RouteTarget`（模型服务器路由目标）、执行角色、模型 revision 和精确 DP rank。

执行阶段和 E/P/D 关联路由组件集收窄（同一组关联的 Encoder、Prefill 和 Decode 路由组件）仍由 Router 负责，在评分后、Picker 前执行。算法可比较完整的兼容健康快照，但不能选择当前阶段收窄范围以外的候选路由项。每个候选路由项还携带 Router 在该轮构造的不可变观测：当前 admitted load 和并发上限、可选 scheduler/KV gauge，以及 Router 统一观测窗口上的吞吐和延迟统计。Filter 和 Scorer 只消费该快照，不再查询 target stats；只有与 request prompt 相关的 KV-prefix lookup 仍是算法查询。

`data_parallel_size: 1` 的 `RouteTarget`（模型服务器路由目标） 只产生 rank `0` 候选路由项，最终决策仍显式返回 `data_parallel_rank: 0`。更大的 RouteTarget 会为每个 rank 产生一个候选路由项。

## 自定义 Context 示例

`RouterPipeline::with_customized_context` 为每个请求创建一个独立的 `C`。Router 在该请求的每轮选择中，将同一个 `&mut C` 依次传给 Filter、Scorer 和 Picker；它会贯穿 initial、Prefill 和 Decode，请求处理结束后释放，不会与其他请求共享。

```rust
let pipeline = RouterPipeline::with_customized_context(
    Arc::new(ContextFilter),
    Arc::new(ContextScorer),
    Arc::new(ContextPicker),
    |request| RoutingContext { request_id: request.generate_request.request_id.clone(), rounds: 0 },
);
let router = PipelineRouter::with_pipeline(inventory, pipeline);
```

例如 Filter 增加 `rounds`，Scorer 读取当前轮次应用评分策略，Picker 再使用同一状态完成选择。可执行行为覆盖位于 `tests/router/pipeline.rs`；不需要 request-local 状态的算法继续使用默认 `RouterPipeline::new` 和 `()` Context。

## 示例

```text
ModelGroups：
  llama3-serve-r-2gosa7pa2jpf2-0  UID 2f48f8e1-7f89-4eb8-bf31-e6d482504f66
  llama3-serve-r-2gosa7pa2jpf2-1  UID 8c88ee9a-c10f-41fd-98ef-a09d256b5213

候选：
  2f48f8e1-7f89-4eb8-bf31-e6d482504f66 / rank 0  KV 命中：  0 tokens
  2f48f8e1-7f89-4eb8-bf31-e6d482504f66 / rank 1  KV 命中：512 tokens
  8c88ee9a-c10f-41fd-98ef-a09d256b5213 / rank 0  KV 命中：256 tokens

Filter：保留索引 0、1、2
Scorer：索引 0 → 0，1 → 512，2 → 256
Picker：选择索引 1

RouteDecision：
  route_target_id: 2f48f8e1-7f89-4eb8-bf31-e6d482504f66
  data_parallel_rank: 1
```

ModelGroup 名称遵循 `<pool-name>-<revision>-<ordinal>`。Router 使用 Kubernetes ModelGroup UID 作为路由身份，而不是使用 `metadata.name`；Deployment、Service 和 Service DNS endpoint 使用 ModelGroup 名称。

Aggregate 和 Prefill 的内置 KV 评分按以下顺序做字典序比较：完整 prompt prefix 命中长度、`Device > HostPinned > Disk > External`、`Local > Remote`，最后比较负载。Decode 的 prefix、tier 和 locality 分数为零。Unavailable KV facts 不会被当作确认 miss。

## 使用 KV prefix indexer

Filter 和 Scorer 都会接收 `&dyn KvPrefixIndexer`。KV 感知算法需要使用候选项的精确 ModelGroup identity 和 DP rank，为每个 candidate 构造查询：

```rust
use foretoken_kv_indexer::{KvPrefixLookup, KvPrefixQueryResult};

let result = KvPrefixLookup::from_generate_request(
    candidate.route_target_id.as_str(),
    candidate.data_parallel_rank,
    request.generate_request.as_ref(),
)
.map_or_else(KvPrefixQueryResult::Unavailable, |lookup| {
    kv_prefix_indexer.prefix_matches(lookup)
});

let matched_tokens = match result {
    KvPrefixQueryResult::Matches(matches) => matches
        .into_iter()
        .map(|matched| matched.matched_tokens)
        .max()
        .unwrap_or(0),
    KvPrefixQueryResult::Unavailable(_) => 0,
};
```

`Unavailable` 不是确认的 cache miss，不能仅据此删除 candidate。Filter 返回输入 candidate 列表中的索引；Scorer 必须按原顺序为每个输入 candidate 返回一个 `RouteScore`。内置的 tier、locality 和负载策略可参考 `src/algorithm/scorer/kv_least_loaded_scorer.rs`。

## 编译期算法注册

Filter、Scorer 和 Picker 实现通过 `inventory::submit!` 在编译期自行注册。Pipeline 配置使用稳定的 lower_snake_case 名称：内置 Filter 是 `allow_all`；Scorer 是 `uniform`、`least_loaded` 和 `kv_least_loaded`；Picker 是 `max` 和 `round_robin`。Router 在启动时校验编译进二进制的 descriptor 与配置；空名、重名和未知名称都是明确错误，绝不静默回退。

社区算法只需在对应的 `src/algorithm/{filter,scorer,picker}/` 目录增加 Rust 实现、实现该类别 trait，并在文件中放置带稳定名称和 factory 的 `inventory::submit!` descriptor。再在该类别的 `mod.rs` 加一行 `mod my_algorithm;`，让 Rust 编译该模块。无需修改中央配置目录，不使用源码扫描、runtime plugin loader、`build.rs` 或 codegen。
