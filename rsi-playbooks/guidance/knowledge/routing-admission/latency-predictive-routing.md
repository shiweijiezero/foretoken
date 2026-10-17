<!--
SPDX-License-Identifier: Apache-2.0
SPDX-FileCopyrightText: Copyright contributors to the Foretoken project
-->

# Reduce Queueing: Predictive Routing and Dispatch Reservations

English | [简体中文](latency-predictive-routing_zh.md)

## Observations and Motivation

Queue depth counts waiting requests, not the work ahead of a new arrival. Three uncached 16K-token prompts can be more expensive than ten 128-token prompts. Delayed engine telemetry creates another failure: a burst can repeatedly choose an endpoint that looked idle before its first dispatch.

## Principles and Analysis

`finish_estimate = current_wait_estimate + incremental_service_estimate`.

Separate uncached input, existing context lengths, decode population and worker configuration. A long prefix hit can reverse the ranking between otherwise unequal queues. Preble uses profiled token-dependent costs, recent load and eviction costs; DualMap uses pending prefill tokens within two candidates. Calibration is specific to model, dtype, backend, topology and parallelism.

Dispatch reservations represent work not yet visible in engine observations. Summing measured load and reservations that are already included double-counts it. Reconciliation therefore needs one lifecycle owner and an explicit observation boundary.

## Design

Predict incremental latency for each eligible worker and immediately record unseen dispatched work. The engine publishes execution/cache observations; the request layer owns selection, reservations and release. Foretoken's request ledger is the relevant accounting owner; cluster orchestration changes capacity.

Dynamo already scores prefill work, cache overlap, decode cost and active requests. Production Stack uses in-prefill plus in-decoding request counts; AIBrix exposes Preble. llm-d has an XGBoost prediction sidecar, lifecycle hooks and endpoint TTFT/TPOT headroom, so predictive routing is more than replacing least-request selection.

## Implementation

1. Combine each worker's sampled state with outstanding dispatch work not represented by that sample.
2. Estimate request-specific TTFT/TPOT and SLO headroom using compatible streaming labels and the worker's calibrated execution configuration.
3. In llm-d, group endpoints by predicted headroom. With all predictions missing, use the KV/queue/prefix composite fallback. With all predictions unfavorable, prefer idle endpoints or a less-severe deficit bucket; this remains ranking, not rejection.
4. Select the destination and reserve its incremental work before another dispatch reads load.
5. Reconcile the reservation when execution observations include it; release remaining lifecycle accounting on completion, error or cancellation.

In the cited llm-d scorer, partially missing predictions can stay in the nonnegative group with zero headroom, unlike the all-missing fallback. Non-streaming TTFT training labels use EOS and effectively measure completion latency, with TPOT neutralized; they cannot be treated as streaming first-token labels.

## Impact and Results

Work-based prediction distinguishes heterogeneous queues and cache-dependent incremental cost. Reservations reduce herd behavior during telemetry gaps without duplicating the engine's measured work. Prediction overhead can outweigh benefits at low load, while changed kernels or stale state can reverse rankings even with small mean regression error. Explicit missing-data and label semantics preserve the meaning of routing decisions.

## References

- [Preble v2, 2024-10-03](https://arxiv.org/html/2407.00023v2) and [DualMap v1, 2026-02-06](https://arxiv.org/html/2602.06502v1): cost prediction at routing boundaries.
- [llm-d predicted-latency producer, commit 5253f555](https://github.com/llm-d/llm-d-inference-scheduler/blob/5253f55516f496129248766d1e4b745daba68887/pkg/epp/framework/plugins/requestcontrol/dataproducer/predictedlatency/README.md): lifecycle hooks and label semantics.
- [llm-d latency scorer code, same revision](https://github.com/llm-d/llm-d-inference-scheduler/blob/5253f55516f496129248766d1e4b745daba68887/pkg/epp/framework/plugins/scheduling/scorer/latency/plugin.go): actual grouping and fallback behavior.
- [Dynamo selector, commit 7708f041](https://github.com/ai-dynamo/dynamo/blob/7708f041a965e35374af976ab8ad610b9537e3bc/lib/kv-router/src/scheduling/selector/reference.rs).
- [Production Stack routing, commit 4e076e8d](https://github.com/vllm-project/production-stack/blob/4e076e8de3698a3729b575e3a5ade65ffbb929cf/src/vllm_router/routers/routing_logic.py).
- [AIBrix routing documentation, commit d0500b03](https://github.com/vllm-project/aibrix/blob/d0500b0399cf35ad0bf95d073faa83c23308f463/docs/source/features/gateway-routing/prefix-cache-aware-routing.rst).
