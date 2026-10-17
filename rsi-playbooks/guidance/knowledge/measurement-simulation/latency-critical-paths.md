<!--
SPDX-License-Identifier: Apache-2.0
SPDX-FileCopyrightText: Copyright contributors to the Foretoken project
-->

# Diagnose latency bottlenecks through causal trace-path reconstruction

English | [简体中文](latency-critical-paths_zh.md) · [Knowledge](../README.md)

## Observations and Motivation

Summing operator durations does not recover request latency: CPU launches can return before device work ends, streams overlap, parent spans include children, and collectives can wait for late ranks. Sorting this sum can prioritize a long off-path task while missing the short launch or synchronization that blocks the selected completion event.

## Principles and Analysis

Interval union removes double-counted overlap: `|A∪B|=|A|+|B|−|A∩B|`, but not causal ambiguity. For a dependency DAG, operation v with duration dᵥ and release rᵥ finishes at `cᵥ=dᵥ+max(rᵥ, max cᵤ)` over predecessors u. The longest required path controls completion under the modeled resource ordering.

As an explanatory example, 4 ms compute overlaps 3 ms communication before a dependent 1 ms stage. Elapsed time is 5 ms, not 8 ms; reducing communication to 2 ms does not shorten it, while reducing compute to 2 ms gives 4 ms. Real overlap can lengthen both operations through resource competition.

## Design

Choose the sink explicitly: first engine token, visible text, final HTTP event, or post-cancellation KV release. Retain process, thread, rank, stream, step, and correlation identity. Build edges from actual launch correlation, stream order, events, and consumer dependencies, not temporal proximity.

Represent observed resource waits separately from execution and retain scheduling order where a shared resource serialized work. Cross-rank edges require collective identity and aligned clocks. Startup/compilation/capture windows differ from steady-state steps; graph-level traces can hide internal nodes.

## Implementation

Two computations answer different questions:

```text
busy_union(intervals):
    sort by start
    merge intervals whose start <= current_end
    sum merged lengths
critical_path(nodes, edges, sink):
    restrict to ancestors of sink
    for v in topological order:
        u = predecessor with greatest completion, if any
        start[v] = release[v] if no u else max(release[v], completion[u])
        completion[v] = start[v] + duration[v]
        parent[v] = u only if u determines start[v]
    backtrack parent from sink; retain releases/waits on the path
```

Split inclusive spans into self work and children before assigning costs. Include measured host descheduling and unresolved-event waits so a DAG does not silently treat idle time as free. Compare reconstructed completion with the observed sink to locate missing coverage. Nsight correlation and Perfetto interval queries support this analysis; Foretoken PyTorch, Nsight, and mcTracer captures have different coverage. Missing cross-process or device correlation remains missing, not an inferred edge.

## Impact and Results

The union calculation distinguishes real occupied wall time from overlap/nesting. The DAG distinguishes exposed dependencies from hidden or off-path work, and rank arrival records distinguish rendezvous waiting from transfer. This avoids overstating speedup from operator totals and identifies the stage whose completion must move earlier. Fine-grained tracing adds overhead, while a graph with incomplete resource or frontend coverage understates the observed path.

## References

- [Nsight Systems User Guide](https://docs.nvidia.com/nsight-systems/UserGuide/index.html): CPU/CUDA correlation, process scope, graph granularity, overhead, and clock alignment.
- [Perfetto Trace Processor](https://perfetto.dev/docs/analysis/trace-processor): slice timestamps/durations and query processing. Interval queries do not themselves identify causal edges.
- [SGLang overlap utilities](https://github.com/sgl-project/sglang/blob/e9400c5a8f3a0d0312a37aecef694f5edfd69341/python/sglang/srt/managers/overlap_utils.py): explicit publication, device events, asynchronous copies, and consumer fences.
- [Foretoken profiling guide](../../../../benchmarks/docs/profile/README.md): at `e6ae3df6`; capture-engine preparation, retained results, and separate unprofiled performance runs.
