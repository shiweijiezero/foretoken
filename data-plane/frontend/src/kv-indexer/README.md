<!-- SPDX-License-Identifier: Apache-2.0 -->
<!-- SPDX-FileCopyrightText: Copyright contributors to the Foretoken project -->

# KV Prefix Index

English | [中文](README_zh.md)

Provides KV prefix matches to Router filters and scorers:

- Local accelerator cache: `Device/Local`.
- Native CPU and filesystem offload: `HostPinned/Local` and `Disk/Local`.
- Mooncake shared memory and SSD cache: `External/Remote`.

## Calling from a routing algorithm

A `RouteFilter` or `RouteScorer` that uses KV matches declares:

```rust
fn needs_kv_prefix(&self) -> bool {
    true
}
```

`PipelineRouter::start` calls `KvPrefixIndexer::prepare` asynchronously before selection. The algorithm then uses the supplied reader synchronously in `filter` or `score`:

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

Each match provides `placement` and the reusable prefix length `matched_tokens`, including any local prefix before a restore. Compare these lengths rather than adding them across tiers. An empty `Matches` means no matching prefix was found; `Unavailable` means the result is unknown. Keep unavailable candidates eligible for ordinary routing.

The built-in [KvLeastLoadedScorer](../router/src/algorithm/scorer/kv_least_loaded_scorer.rs) combines GPU-local and restored prefix affinity with request load. See [PipelineRouter](../router/src/selection/pipeline_router.rs) for batched query preparation.

## Observe

Use the frontend's `/statusz` for event-index health and `/metrics` for monitoring. See [frontend endpoint access](../../README.md#operations).
