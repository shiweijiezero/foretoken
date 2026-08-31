<!-- SPDX-License-Identifier: Apache-2.0 -->
<!-- SPDX-FileCopyrightText: Copyright contributors to the Foretoken project -->

# Router

## What problem the Router solves

The same model can be served by multiple ModelGroups, and each ModelGroup may contain multiple DP ranks. For each request, the Router selects an exact route target and DP rank and returns a `RouteDecision`.

The Router does not run model inference. It only selects a target from the currently available routes using request requirements, KV cache matches, load, and other routing information.

## How a request is routed

```text
RouterRequest
    ↓
Compatible and healthy route candidates
    ↓ Filter
Retained candidates
    ↓ Scorer
Scored candidates
    ↓ Picker
RouteDecision
```

### Route candidates

The Router obtains route targets from the `RouteInventory`. A target becomes a candidate only when its model, input limit, and request capabilities are compatible and the target is healthy. Its execution role remains part of the candidate for later stage selection.

Dynamic capability-aware matching deeply inspects target node `capabilities` to satisfy advanced requests:

- **Model & Basic Limits**: Strictly matches the request's `model` and ensures its token count does not exceed the node's `max_input_tokens`.
- **LoRA & Reasoning**: Accurately identifies whether target nodes possess the corresponding `lora` or `reasoning` parsing capabilities.
- **Multimodal**: Automatically extracts multimodal features from requests and strictly matches whether nodes support the corresponding sub-modalities.
- **Structured Output**: Perceives and matches mandatory output constraints.

A `RouteTarget` expands according to its `data_parallel_size`:

- `data_parallel_size: 1` produces rank `0`;
- larger values produce one candidate for each DP rank.

Each candidate also contains the load and runtime observations read by the Router for that selection round. Algorithms use this snapshot instead of querying model servers themselves.

### Filter, Scorer, and Picker

The routing pipeline has three replaceable interfaces:

- **Filter** returns the indexes of candidates to retain and can exclude candidates that do not meet a policy.
- **Scorer** returns one `RouteScore` for every retained candidate in the same order.
- **Picker** selects an index from the current scored list.

All three interfaces use indexes instead of creating or returning new candidates. The Router keeps ownership of the candidates, so an algorithm cannot change route identity or select a target outside the current list. Duplicate or out-of-range indexes and score-count mismatches are explicit routing errors.

### Built-in routing algorithm policies (Planned)

The system includes or plans the following compile-ready policy implementations:

**Filters:**

- `allow_all`: Default pass-through filter. Retains all healthy, basically compatible candidate nodes to enter the scoring stage without extra interception.

**Scorers:**

- `kv_least_loaded`: KV Cache locality and low-load priority policy. Evaluation dimensions in priority order:
  - Matched token count: Prefers the longest KV matched prefix.
  - Storage tier preference: Device (4) > HostPinned (3) > Disk (2) > External (1).
  - Physical locality preference: Local (2) > Remote (1).
  - Current load: When prefix conditions match, prefers lower overall load (including downstream Decode nodes).
- `least_loaded`: Absolute low-load priority policy. Uses the larger of model-server admitted requests and vLLM scheduler running-plus-waiting requests, and automatically aggregates downstream associated loads.
- `uniform`: Uniform scoring policy. Assigns the same score to all candidate nodes, typically paired with `round_robin` for random or round-robin routing.
- `weighted_round_robin`: Weighted round-robin based on preset node weights (GPU compute power, VRAM size, etc.), allowing higher-performance nodes to handle more requests.
- `lowest_latency`: Scores based on target nodes' recent average response latencies (such as TTFT - Time to First Token, or TPOT - Time Per Output Token), prioritizing the fastest responding nodes.
- `multi_tenant`: Multi-tenant rate limiting, planned to be implemented via a combination of Filter and Scorer.

**Pickers:**

- `max`: Selects the node with the highest score. In case of ties, uses the smallest `route_target_id` as a deterministic tie-breaking condition.
- `round_robin`: Rotates among all nodes tied for the maximum score, utilizing atomic operations to ensure requests are evenly distributed across equally excellent tied nodes under high concurrency.

## A complete example

Suppose two ModelGroups are healthy:

```text
Candidates:
  ModelGroup A / rank 0  KV match:   0 tokens
  ModelGroup A / rank 1  KV match: 512 tokens
  ModelGroup B / rank 0  KV match: 256 tokens

Filter: retain indexes 0, 1, and 2
Scorer: produce one score for each candidate
Picker: select index 1

RouteDecision:
  route_target_id: ModelGroup A
  data_parallel_rank: 1
```

`route_target_id` is the stable routing identity supplied by the Model Server Registry. `RouteDecision` also returns the execution role, model, and exact DP rank.

## Using KV prefix information

Filter and Scorer receive a `&dyn KvPrefixIndexer`. A KV-aware algorithm queries reusable prompt prefixes using the candidate's route target and DP rank:

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

`Unavailable` means the index cannot provide a reliable answer. It is not a confirmed cache miss and must not by itself remove a candidate. See [`../kv-indexer/README.md`](../kv-indexer/README.md) for the KV prefix indexer's responsibilities and extension model.

## Adding a custom algorithm

### Implement an algorithm interface

Implement `RouteFilter`, `RouteScorer`, or `RoutePicker` as needed. An implementation should use the request, candidates, and observation snapshot provided by the Router. It should not modify candidates or maintain a second route catalog inside the algorithm.

Community algorithms belong in the corresponding directory:

```text
src/algorithm/filter/
src/algorithm/scorer/
src/algorithm/picker/
```

### Register the algorithm

Use `inventory::submit!` to register a stable name and factory at compile time, then declare the module in the corresponding directory's `mod.rs`:

```rust
mod my_algorithm;
```

No central algorithm catalog, source scanning, runtime plugin loader, `build.rs`, or code generation is required. The Router validates configured algorithm names at startup; empty, duplicate, and unknown names return explicit errors.

## Request-local Context

Most algorithms need no additional state and can use `RouterPipeline::new`. When Filter, Scorer, and Picker need to share state within one request, use `RouterPipeline::with_customized_context`:

```rust
let pipeline = RouterPipeline::with_customized_context(
    Arc::new(ContextFilter),
    Arc::new(ContextScorer),
    Arc::new(ContextPicker),
    |_| RoutingContext { rounds: 0 },
);
```

The Router creates one Context for each request and passes it through Filter, Scorer, and Picker in every selection round. The Context is dropped when the request ends and is never shared with another request.

## E/P/D multi-stage routing

A request may be served by associated Encoder, Prefill, and Decode route components. The Router selects a target for each required execution stage and keeps those targets within the same pipeline scope.

Built-in load-aware scorers possess pipeline visibility: when scoring for the Prefill stage, the scorer automatically factors in the load of the lightest Decode node within that E/P/D path, achieving load balancing across the entire pipeline.

Algorithms may score the complete compatible and healthy candidate snapshot. After scoring and before Picker, the Router narrows the list for the current execution stage and the selected pipeline scope. Picker therefore cannot select a target outside the associated component set. The same request-local Context remains available across these selection stages.
