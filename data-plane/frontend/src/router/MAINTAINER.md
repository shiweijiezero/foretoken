<!-- SPDX-License-Identifier: Apache-2.0 -->
<!-- SPDX-FileCopyrightText: Copyright contributors to the Foretoken project -->

# Router Maintenance

English | [中文](MAINTAINER_zh.md)

Router algorithms are compiled into the Frontend binary. They are not runtime plugins and are not a public extension surface.

## Pipeline contracts

Each request is processed as:

```text
compatible and healthy candidates
→ Filter indexes
→ Scorer scores parallel to retained candidates
→ Picker index
→ RouteDecision
```

- `RouteFilter` returns indexes of candidates to retain.
- `RouteScorer` returns one `RouteScore` for every retained candidate in the same order.
- `RoutePicker` returns an index into the scored candidates.

The Router owns candidate identity and validates duplicate or out-of-range indexes and score-count mismatches. Algorithms must not maintain a second route catalog or query model servers on the request path; they receive an immutable round-local observation snapshot. Filters and scorers that consume shared KV matches return `true` from `needs_kv_prefix`; `Router::start` then prepares those observations asynchronously through the KV indexer before running the synchronous pipeline.

## Adding an algorithm

Implement the appropriate interface under `src/algorithm/filter/`, `src/algorithm/scorer/`, or `src/algorithm/picker/`. Add one entry to the corresponding stage's `declare_router_algorithms!` list in `mod.rs`, providing the module name, type name, and user-facing configuration name. The macro generates the module declaration, public re-export, and compiled descriptor registration; no Controller enum or CRD change is required. Update maintained examples, the user-facing Router README, and contract tests only when observable behavior changes.

Request-local shared state belongs in `RouterPipeline::with_customized_context`. The Router creates one context per request and drops it when that request finishes. A joint E/P/D implementation can evaluate the complete candidate snapshot during the initial round, retain its preferred stage identities in this context, and have its Picker return one planned candidate in each stage.

## Multi-stage routing

Algorithms score the complete compatible and healthy candidate snapshot. Before picking, the Router narrows it to the current execution stage and its selected controller-defined connector compatibility scope. The scope may contain multiple Encoder, Prefill, and Decode ModelGroups; it protects the runtime transfer contract without imposing ordinal pairing. Picker still selects one candidate per stage, while request-local context can carry a joint plan across rounds.

## Scorer contracts

`least_loaded` and `kv_least_loaded` share the maximum of Model Server active requests, scheduler
running plus waiting requests, and frontend-local active requests for the candidate's exact DP rank.
These counts overlap and must not be summed. Missing telemetry leaves the local count usable;
endpoint telemetry is shared across ranks and does not provide per-rank engine load.
Prefill's downstream Decode comparison uses the same calculation within its pipeline scope.

Scorers use the following observations and formulas:

| Scorer | Input | Score |
| --- | --- | --- |
| `queue_depth` | `scheduler_waiting_requests` | `(max - waiting) / (max - min)` |
| `running_request` | `scheduler_running_requests` | `(max - running) / (max - min)` |
| `kv_cache_utilization` | `kv_cache_usage` | `1 - usage` |
| `token_load` | In-flight tokens plus incoming uncached prompt tokens, `load` | `1` if `load <= 0`; otherwise `1 - min(load, threshold) / threshold` |

`queue_depth` and `running_request` counts normalize over all candidates supplied to `score`; equal counts receive `1`,
and an empty candidate slice produces an empty score vector. Count subtraction precedes
conversion to `f64`, preserving differences between large adjacent counts.
`RouteScore.preference` preserves the numeric output, with the
locality/load fields left at zero. Existing locality policies retain their lexicographic ordering.

The registry owns gauge history: it publishes gauges immediately and uses the last measured value
on omission only while that raw snapshot remains retained. A non-increasing timestamp or a
cumulative counter or histogram reset clears history. Omitted gauges in the new history remain
unobserved until reported. Metric scorers use zero for unobserved gauges.
Rates and windowed latencies remain unavailable until their counter window is covered.

Foretoken handles telemetry transport, health checks, DP expansion, and E/P/D eligibility.
Its Model Server endpoint reports sums of scheduler counts and mean KV utilization across its
engines.
Every rank of that endpoint receives the same metric score. These three metric scorers ignore
`RoutingProgress`; Router still supplies it and owns the subsequent stage selection.

`token_load` adds signed token counts before conversion to `f64`. `threshold` is `queueThresholdTokens`
(default `4194304`); nonpositive values use the default. Uncached tokens include the partial prompt tail;
without usable cache observations, the whole prompt counts. Output tokens are not estimated.
Aggregate, Prefill, and Decode reserve uncached prompt tokens before dispatch and release them on the
first response, stage completion, or session drop, including failed dispatches.

`token_load` uses frontend-local reservations per target and DP rank, without engine scheduler gauges
or other frontend replicas' requests. Selection and reservation share one lock; routing sessions own
cleanup, and RuntimeBuilder retains the state across serving-snapshot replacements.

Set optional parameters in `FrontendService.spec.routerPipeline.scorerParameters`; the selected scorer reads them at frontend startup.
