<!--
SPDX-License-Identifier: Apache-2.0
SPDX-FileCopyrightText: Copyright contributors to the Foretoken project
-->

# Reduce TTFT: Balance Prefix Reuse Against Queueing

English | [简体中文](ttft-cache-queue-balance_zh.md)

## Observations and Motivation

A prefix hit saves compute only after the worker runs the request. Sending every shared-prefix arrival to the warmest replica can build a queue longer than the prefill saved. The optimization target is user-visible TTFT, not maximum cache-hit percentage.

## Principles and Analysis

`estimated_TTFT = ingress_wait + worker_wait + cache_ready_time + uncached_prefill_time + response_overhead`.

This is an analysis decomposition, not every router's exact formula. Advertised state may need host or remote restoration before scheduling, so hit length alone does not describe cache readiness.

For a 10K-token input, suppose A reuses 9K tokens and needs 30 ms of prefill, while B misses and needs 220 ms. With waits of 300 ms and 20 ms, B's estimated 240 ms beats A's 330 ms. If A's wait drops to 40 ms, A wins. These illustrative costs show why affinity must compete with queueing.

## Design

Compare reuse against incremental work and load among eligible candidates. Preble estimates recent compute, new prefill and eviction cost. DualMap restricts each request to two hash-derived candidates, favors reuse, and switches toward lower load when estimated TTFT violates the SLO. Adaptive prefix keys spread hot families while retaining cold-prefix locality; migrations target the predetermined backup only when estimated benefit is positive.

The engine owns cache validity, allocation and iteration scheduling. The request layer owns eligibility, scoring and dispatch accounting; orchestration owns placement/capacity. Local frontend observations retain their local scope.

## Implementation

1. Filter healthy, compatible candidates and obtain their reusable-prefix and load observations.
2. Separate GPU-ready reuse from state requiring restoration; estimate the uncached suffix and queueing cost.
3. Score the trade-off using the chosen mechanism. Dynamo uses block-based prefill/decode cost and overlap credit, decaying device credit when backlog exceeds the least-loaded eligible worker. Its decay normalizes excess backlog by incoming request size, not milliseconds.
4. Select and account for the dispatch immediately so the next request sees its added pressure.

AIBrix blends normalized prefix/load scores (`5 × prefix + 4 × load_balance` in the cited default) or uses load-qualified cache-first selection. Production Stack subtracts relative in-flight request load from matched-token ratio. Foretoken's `kv_least_loaded`, `prefix`, `token_load` and `two_tier` express routing preferences, not calibrated transfer times; llm-d also supplies composable prefix/queue/load/latency scoring.

## Impact and Results

Balancing cache and queue cost reduces affinity hotspots without discarding cheap reuse. At low load, warm placement often wins; under contention, an uncached worker can deliver the first token sooner. Request-count load can miss large prefill differences, while eviction, stale observations and unequal worker speeds can reverse rankings. Session affinity preserves placement but does not prove the session's KV remains resident.

## References

- [Preble, 2407.00023v2, 2024-10-03](https://arxiv.org/html/2407.00023v2): execution-time-aware exploitation/exploration and eviction cost.
- [DualMap, 2602.06502v1, 2026-02-06](https://arxiv.org/html/2602.06502v1): two-candidate routing and SLO-aware cache/load fallback.
- [Dynamo selector, commit 7708f041](https://github.com/ai-dynamo/dynamo/blob/7708f041a965e35374af976ab8ad610b9537e3bc/lib/kv-router/src/scheduling/selector/reference.rs): implemented backlog and overlap-credit decay.
- [AIBrix prefix routing, commit d0500b03](https://github.com/vllm-project/aibrix/blob/d0500b0399cf35ad0bf95d073faa83c23308f463/docs/source/features/gateway-routing/prefix-cache-aware-routing.rst).
- [Production Stack routing, commit 4e076e8d](https://github.com/vllm-project/production-stack/blob/4e076e8de3698a3729b575e3a5ade65ffbb929cf/src/vllm_router/routers/routing_logic.py).
- [llm-d prefix scorer, commit 5253f555](https://github.com/llm-d/llm-d-inference-scheduler/blob/5253f55516f496129248766d1e4b745daba68887/pkg/epp/framework/plugins/scheduling/scorer/prefix/README.md).
- [Foretoken routing](../../../../data-plane/frontend/src/router/README.md).
