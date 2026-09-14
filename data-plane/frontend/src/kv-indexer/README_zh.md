<!-- SPDX-License-Identifier: Apache-2.0 -->
<!-- SPDX-FileCopyrightText: Copyright contributors to the Foretoken project -->

# KV 前缀索引

[English](README.md) | 中文

为 Router 的 Filter 和 Scorer 提供 KV 前缀匹配：

- 本地加速器缓存：`Device/Local`。
- Mooncake 共享内存与 SSD 缓存：`External/Remote`，支持单 DP 文本请求。

## 在路由算法中调用

需要共享 KV 匹配的 `RouteFilter` 或 `RouteScorer` 声明：

```rust
fn needs_kv_prefix(&self) -> bool {
    true
}
```

`PipelineRouter::start` 会在选择目标前异步调用 `KvPrefixIndexer::prepare`。算法在 `filter` 或 `score` 中同步使用传入的查询器：

```rust
use foretoken_kv_indexer::{KvPrefixIndexer, KvPrefixQueryResult};
use foretoken_router::{RouteCandidate, RouterRequest};

fn candidate_prefix(
    request: &RouterRequest,
    candidate: &RouteCandidate,
    indexer: &dyn KvPrefixIndexer,
) -> KvPrefixQueryResult {
    match request.kv_prefix_lookup(
        &candidate.route_target_id,
        candidate.data_parallel_rank,
    ) {
        Ok(lookup) => indexer.prefix_matches(lookup),
        Err(reason) => KvPrefixQueryResult::Unavailable(reason),
    }
}
```

每项匹配包含缓存位置 `placement` 和匹配长度 `matched_tokens`。空的 `Matches` 表示未命中，`Unavailable` 表示无法判断；无法判断的候选仍可参与常规路由。

内置的 [KvLeastLoadedScorer](../router/src/algorithm/scorer/kv_least_loaded_scorer.rs) 优先比较匹配长度，再比较缓存层级和负载。批量准备查询的调用见 [PipelineRouter](../router/src/selection/pipeline_router.rs)。

## 查看状态

通过前端的 `/statusz` 查看索引健康状态，通过 `/metrics` 监控。访问方式见[前端接口访问范围](../../README_zh.md#接口访问范围)。
