<!-- SPDX-License-Identifier: Apache-2.0 -->
<!-- SPDX-FileCopyrightText: Copyright contributors to the Foretoken project -->

# Router

For each execution stage, the Router selects one routable ModelGroup (that is, an engine-core-server) and an exact DP rank.

Its pipeline has three list-level interfaces:

- **Filter** receives the compatible, healthy eligible route-option snapshot (a candidate) and returns indexes for a retained subset. It cannot create or modify candidates. Out-of-range or duplicate indexes are explicit routing errors.
- **Scorer** returns one `RouteScore` for every retained eligible route option, in the same order. The Router owns the candidate/score view; a score-count mismatch is an explicit routing error. `KvLeastLoadedScorer` compares matched prompt tokens, storage tier, locality, and load.
- **Picker** selects an index in the current scored list rather than returning an eligible route option. An out-of-range index, or `None` for a nonempty list, is an explicit routing error. The Router then exposes a `RouteDecision` containing the ModelGroup `RouteTarget` (the model-server route destination), execution role, model revision, and exact DP rank.

Stage and E/P/D linked route-set narrowing (the same group of associated Encoder, Prefill, and Decode route components) remain Router-owned and run after scoring, before Picker. Algorithms can compare the complete compatible, healthy snapshot but cannot choose outside the stage's narrowed eligible route options. Each eligible route option also carries the Router's immutable observation for that routing round: current admitted load and concurrency, optional scheduler/KV gauges, and throughput and latency statistics over the Router-owned observation window. Filter and Scorer consume that snapshot rather than querying target statistics; only request-dependent KV-prefix lookup remains an algorithm query.

A `RouteTarget` (a model-server route destination) with `data_parallel_size: 1` contributes only rank `0`, so its decision explicitly returns `data_parallel_rank: 0`. Larger targets contribute one eligible route option per rank.

## Customized Context case

`RouterPipeline::with_customized_context` creates one owned `C` for each request. The Router passes `&mut C` to Filter, Scorer, and Picker in every selection round; the same value lives through the initial, Prefill, and Decode selections, then is dropped when request processing ends. It is not shared with another request.

```rust
let pipeline = RouterPipeline::with_customized_context(
    Arc::new(ContextFilter),
    Arc::new(ContextScorer),
    Arc::new(ContextPicker),
    |request| RoutingContext { request_id: request.generate_request.request_id.clone(), rounds: 0 },
);
let router = PipelineRouter::with_pipeline(inventory, pipeline);
```

For example, Filter increments `rounds`, Scorer reads that round to attach scoring policy, and Picker consumes the same state to make its final choice. The executable behavior is covered by `tests/router/pipeline.rs`; the default `RouterPipeline::new` remains the `()` convenience for algorithms that need no request-local state.

## Example

```text
ModelGroups:
  llama3-serve-r-2gosa7pa2jpf2-0  UID 2f48f8e1-7f89-4eb8-bf31-e6d482504f66
  llama3-serve-r-2gosa7pa2jpf2-1  UID 8c88ee9a-c10f-41fd-98ef-a09d256b5213

Candidates:
  2f48f8e1-7f89-4eb8-bf31-e6d482504f66 / rank 0  KV match:   0 tokens
  2f48f8e1-7f89-4eb8-bf31-e6d482504f66 / rank 1  KV match: 512 tokens
  8c88ee9a-c10f-41fd-98ef-a09d256b5213 / rank 0  KV match: 256 tokens

Filter:  retain indexes 0, 1, and 2
Scorer:  score indexes 0 → 0, 1 → 512, 2 → 256
Picker:  select index 1

RouteDecision:
  route_target_id: 2f48f8e1-7f89-4eb8-bf31-e6d482504f66
  data_parallel_rank: 1
```

ModelGroup names follow `<pool-name>-<revision>-<ordinal>`. Router identity uses the Kubernetes ModelGroup UID rather than `metadata.name`; the name is used by the Deployment, Service, and service DNS endpoint.

For Aggregate and Prefill, `KvLeastLoadedScorer` is lexicographic: longest complete matched prompt prefix first, then `Device > HostPinned > Disk > External`, then `Local > Remote`, then lower load. Decode prefix, tier, and locality scores are zero. Unavailable KV facts never become a confirmed miss.

## Using the KV prefix indexer

Filter and Scorer receive a `&dyn KvPrefixIndexer`. A KV-aware algorithm constructs one lookup for each candidate from the candidate's exact ModelGroup identity and DP rank:

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

`Unavailable` is not a confirmed cache miss, so it must not by itself remove a candidate. A Filter returns indexes into its input candidate list. A Scorer returns one `RouteScore` for every input candidate in the same order. See `src/algorithm/scorer/kv_least_loaded_scorer.rs` for the built-in tier, locality, and load policy.

## Compiled algorithm registry

Filter, Scorer, and Picker implementations register themselves at compile time with `inventory::submit!`. Pipeline configuration uses stable lower-snake-case names: the built-ins are `allow_all`; `uniform`, `least_loaded`, and `kv_least_loaded`; and `max` and `round_robin`. The Router validates compiled descriptors and configuration during startup. Empty, duplicate, or unknown names are explicit errors; it never falls back silently.

To add a community algorithm, add its Rust implementation in the appropriate `src/algorithm/{filter,scorer,picker}/` directory, implement the category trait, and place an `inventory::submit!` descriptor with its stable name and factory in that file. Add one `mod my_algorithm;` line to that category's `mod.rs` so Rust compiles the module. No central configuration catalog, source scanning, runtime plugin loader, `build.rs`, or code generation is involved.
