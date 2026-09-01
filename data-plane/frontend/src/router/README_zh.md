<!-- SPDX-License-Identifier: Apache-2.0 -->
<!-- SPDX-FileCopyrightText: Copyright contributors to the Foretoken project -->

# Router

## Router 解决什么问题

同一个模型可以由多个 ModelGroup 提供服务，每个 ModelGroup 还可能包含多个 DP rank。Router 为一次请求选择具体的路由目标和 DP rank，并返回 `RouteDecision`。

Router 不执行模型推理。它只根据当前可用的路由目标、请求要求、KV cache 命中和负载等信息完成选择。

## 一次请求如何完成路由

```text
RouterRequest
    ↓
兼容且健康的候选路由项
    ↓ Filter
保留的候选路由项
    ↓ Scorer
带分数的候选路由项
    ↓ Picker
RouteDecision
```

### 候选路由项

Router 从 `RouteInventory` 获取路由目标。只有模型、输入限制和请求能力兼容且当前健康的目标才会成为候选项。执行角色会保留在候选项中，供后续执行阶段筛选。

为满足高级请求，动态能力感知匹配会深入检查目标节点的 `capabilities`：

- 模型与基础限制：严格匹配请求的 `model`，并确保请求的 token 数不超过节点的 `max_input_tokens`。
- LoRA 与 reasoning：准确识别目标节点是否具备相应的 `lora` 或 `reasoning` 解析能力。
- 多模态：自动提取请求中的多模态特征，并严格匹配节点是否支持相应的子模态。
- 结构化输出：识别并匹配强制性输出约束。

一个 `RouteTarget` 会按 `data_parallel_size` 展开为候选项：

- `data_parallel_size: 1` 产生 rank `0`；
- 更大的值为每个 DP rank 产生一个候选项。

候选项还包含 Router 在本轮读取的负载和运行状态。算法使用这份快照，不需要自行查询模型服务器。

### Filter、Scorer 和 Picker

路由流程由三个可替换的接口组成：

- **Filter** 返回要保留的候选项索引，用于排除不满足策略要求的候选项。
- **Scorer** 按原顺序为每个保留候选项返回一个仅用于比较大小的 `RouteScore`。
- **Picker** 从当前评分列表中选择一个索引。

三个接口都使用索引，而不是创建或返回新的候选项。候选项始终由 Router 持有，因此算法不能修改路由身份或选择列表之外的目标。重复索引、越界索引和分数数量不匹配都会成为明确的路由错误。

### 内置路由算法策略（部分规划中）

系统已经提供或计划提供以下可编译的策略实现：

**过滤器：**

- `allow_all`：默认的直通过滤器。保留所有健康且满足基本兼容条件的候选项，不附加其他过滤条件。

**评分器：**

- `kv_least_loaded`：优先利用可复用的 KV 缓存，并在缓存条件相同时选择负载较低的候选项。比较顺序如下：
  1. **匹配 token 数**：优先选择可复用前缀最长的候选项；
  2. **存储层级**：`Device` (4) > `HostPinned` (3) > `Disk` (2) > `External` (1)；
  3. **位置关系**：`Local` (2) > `Remote` (1)；
  4. **当前负载**：上述条件相同时，优先选择包含下游 Decode 阶段在内的总体负载较低者。
- `least_loaded`：优先选择当前请求负载最低的候选项。基础负载取以下两项中的较大值：
  1. 模型服务器已经接纳但尚未完成的请求数；
  2. vLLM 调度器中正在运行与等待处理的请求总数。
  对于 Prefill/Decode 分离式推理，Prefill 候选项还会加上同一流水线中负载最低的 Decode 候选项负载。
- `uniform`：为所有候选项赋予相同得分，通常与 `round_robin` 组合使用，实现均匀的轮询路由。
- `weighted_round_robin`：基于预设的节点权重（GPU 算力大小、显存容量等）进行加权轮询，让性能更强的节点承担更多请求。
- `lowest_latency`：基于目标节点近期的平均响应延迟，如 TTFT（首个 token 时延）或 TPOT（每个输出 token 的时延）进行评分，优先路由给响应最快的节点。
- `multi_tenant`：多租户限流，计划通过 Filter 与 Scorer 的组合实现。

**选择器：**

- `max`：选择得分最高的节点。出现同分时，选择 `route_target_id` 最小的节点，以便得到确定且可重复的结果。
- `round_robin`：在所有得分最高的节点中进行轮询，利用原子操作确保高并发下的请求能均匀分布到得分相同的最高分节点。

## 一个完整示例

假设有两个健康的 ModelGroup：

```text
候选项：
  ModelGroup A / rank 0  KV 命中 0 tokens
  ModelGroup A / rank 1  KV 命中 512 tokens
  ModelGroup B / rank 0  KV 命中 256 tokens

Filter：保留索引 0、1、2
Scorer：为三个候选项分别生成分数
Picker：选择索引 1

RouteDecision：
  route_target_id: ModelGroup A
  data_parallel_rank: 1
```

`route_target_id` 是 Model Server Registry 提供的稳定路由身份。`RouteDecision` 还会返回执行角色、模型和精确 DP rank。

## 使用 KV 前缀信息

Filter 和 Scorer 都会接收 `&dyn KvPrefixIndexer`。KV 感知算法使用候选项的路由目标和 DP rank 查询可复用的 prompt 前缀：

```rust
use foretoken_kv_indexer::KvPrefixQueryResult;

let result = request
    .kv_prefix_lookup(
        candidate.route_target_id.as_str(),
        candidate.data_parallel_rank,
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

`Unavailable` 表示当前无法可靠查询，不是已确认的缓存未命中，不能仅凭它移除候选项。KV 前缀索引的职责和扩展方式见 [`../kv-indexer/README_zh.md`](../kv-indexer/README_zh.md)。

## 添加自定义算法

### 实现算法接口

根据需要实现 `RouteFilter`、`RouteScorer` 或 `RoutePicker`。实现应只使用 Router 提供的请求、候选项和观测快照，不应修改候选项或在算法内部维护另一份路由目录。

社区算法放在对应目录：

```text
src/algorithm/filter/
src/algorithm/scorer/
src/algorithm/picker/
```

### 注册算法

算法通过 `inventory::submit!` 在编译期注册稳定名称和 factory，并在对应目录的 `mod.rs` 中声明模块：

```rust
mod my_algorithm;
```

不需要修改中央算法清单，也不依赖源码扫描、runtime plugin loader、`build.rs` 或 codegen。Router 启动时会校验配置引用的算法名称；空名称、重复名称和未知名称都会返回明确错误。

## 请求级 Context

多数算法不需要额外状态，可以使用 `RouterPipeline::new`。如果 Filter、Scorer 和 Picker 需要在同一个请求内共享状态，可使用 `RouterPipeline::with_customized_context`：

```rust
let pipeline = RouterPipeline::with_customized_context(
    Arc::new(ContextFilter),
    Arc::new(ContextScorer),
    Arc::new(ContextPicker),
    |_| RoutingContext { rounds: 0 },
);
```

Router 为每个请求创建独立的 Context，并在该请求的每轮选择中依次传给 Filter、Scorer 和 Picker。请求结束后 Context 被释放，不会与其他请求共享。

## E/P/D 多阶段路由

一个请求可能由关联的 Encoder、Prefill 和 Decode 路由组件共同完成。Router 会为每个需要的执行阶段分别选择目标，并确保这些目标属于同一个 pipeline scope (`pipeline_scope_id`)。

内置的负载感知打分器能够感知整条流水线：当为 Prefill 阶段进行评分时，打分器会自动将该 E/P/D 链路中负载最轻的 Decode 节点的负载计算在内，从而实现整条流水线的负载均衡。

算法可以对完整的、兼容且健康的候选项快照进行评分。Router 在评分后、Picker 前，会根据当前执行阶段和已选定的 pipeline scope 收窄候选范围，因此 Picker 不能选择关联组件集之外的目标。同一个请求级 Context 会贯穿这些选择阶段。
