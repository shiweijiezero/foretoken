<!-- SPDX-License-Identifier: Apache-2.0 -->
<!-- SPDX-FileCopyrightText: Copyright contributors to the Foretoken project -->

# KV 前缀索引

[English](README.md) | 中文

为 Router 的 Filter 和 Scorer 提供 KV 前缀匹配：

- 本地加速器缓存：`Device/Local`。
- Prefill 可读取的远端设备 KV 缓存：`Device/Remote`。
- 引擎的 CPU 与文件卸载缓存：`HostPinned/Local`、`Disk/Local`。
- Mooncake 共享内存与 SSD 缓存：`External/Remote`。

这些位置描述 KV 存放在哪里，不绑定具体传输库。NIXL 和 Mooncake 都可以查询和复用 Decode 保留的设备前缀，Mooncake Store 则单独提供共享缓存查询。配置及运行条件见 [P/D 传输与复用](../../../../docs/features/inference-parameters_zh.md#分离预填充与解码并复用-kv)。

## 在路由算法中调用

需要 KV 匹配的 `RouteFilter` 或 `RouteScorer` 声明：

```rust
fn needs_kv_prefix(&self) -> bool {
    true
}
```

Router 会在调用算法前准备观测结果。在 `filter` 或 `score` 中，通过传入的查询器同步读取即可：

```rust
use foretoken_kv_indexer::{KvPrefixIndexer, KvPrefixQueryResult};
use foretoken_router::{RouteCandidate, RouterRequest};

fn candidate_prefix(
    request: &RouterRequest,
    candidate: &RouteCandidate,
    indexer: &dyn KvPrefixIndexer,
) -> KvPrefixQueryResult {
    match request.kv_prefix_lookup(
        candidate.route_target_id.as_str(),
        candidate.data_parallel_rank,
    ) {
        Ok(lookup) => indexer.prefix_matches(lookup),
        Err(reason) => KvPrefixQueryResult::Unavailable(reason),
    }
}
```

每项匹配包含缓存位置 `placement` 和可复用前缀长度 `matched_tokens`；需要恢复缓存时，该长度已包含前面的本地命中，应比较各层结果而不是相加。空的 `Matches` 表示未命中，`Unavailable` 表示无法判断；无法判断的候选仍可参与常规路由。

内置的 [KvLeastLoadedScorer](../router/src/algorithm/scorer/kv_least_loaded_scorer.rs) 综合 GPU 本地前缀、可恢复前缀与请求负载评分。批量准备查询的调用见 [PipelineRouter](../router/src/selection/pipeline_router.rs)。

## 查看状态

通过前端的 `/statusz` 查看事件索引的健康状态，通过 `/metrics` 监控。访问方式见[前端接口访问范围](../../README_zh.md#运维)。
