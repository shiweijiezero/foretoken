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

Scorers use the following observations and formulas:

| Scorer | Input | Score |
| --- | --- | --- |
| `queue_depth` | `scheduler_waiting_requests` | `(max - waiting) / (max - min)` |
| `running_request` | `scheduler_running_requests` | `(max - running) / (max - min)` |
| `kv_cache_utilization` | `kv_cache_usage` | `1 - usage` |

Counts normalize over candidates with measured rank-local gauges; equal measured counts receive `1`,
and an empty candidate slice produces an empty score vector. Count subtraction precedes
conversion to `f64`, preserving differences between large adjacent counts.
`RouteScore.preference` preserves the numeric output, with the
locality/load fields left at zero. Existing locality policies retain their lexicographic ordering.

The registry retains one telemetry history per model group. Group counters and latency windows remain aggregate; the latest snapshot also carries independent scheduler counts and KV utilization for each global DP rank. Rank gauges are read only from that latest snapshot, so missing ranks or fields remain unknown rather than inheriting another rank's values. Unknown observations rank after measured values, without removing candidates. Non-increasing timestamps or reset counters clear the existing history; rates and windowed latencies remain unavailable until their counter window is covered.

Candidate expansion shares the immutable group snapshot; each scorer selects the candidate's exact DP-rank observation. Model Server derives group scheduler totals and mean KV utilization from those same rank measurements. No additional polling or rank-history store is introduced. Router still owns health, DP expansion and E/P/D eligibility.
