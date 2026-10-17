<!--
SPDX-License-Identifier: Apache-2.0
SPDX-FileCopyrightText: Copyright contributors to the Foretoken project
-->

# Size replicas through uncached-token and KV pressure

English | [简体中文](token-pressure-scaling_zh.md)

## Observations and Motivation

Ten 256-token prompts and ten 32K-token prompts have equal queue depth but very different compute and KV demand. Prefix reuse and skew across DP ranks further change capacity needs. Averaging two ranks at 0.95 and 0.15 KV pressure gives a comfortable-looking 0.55 while the hot rank may stop admission.

## Principles and Analysis

Use uncached queued prefill work, scheduled/queued decode contexts, KV capacity, active requests and ready workers with explicit units. Queued tokens divided by calibrated prefill tokens/s estimate backlog seconds; context demand divided by KV token capacity estimates memory pressure, not automatically TTFT. Shared prefixes occupy cache without proportional new prefill, so raw request lengths cannot simply be summed as executed work.

For cross-model service, TRE proposes `TSS_raw = (w_p*r_prefill + r_decode)/(active + w_q*waiting)`. The rates use tokens/s over the same window; request counts form the denominator and weights are dimensionless. Normalize its smoothed value by model/SLO-specific healthy boundary θ. Both raw TSS and θ have weighted tokens/s per weighted outstanding request; their ratio is dimensionless and below 1 signals a calibrated service deficit.

## Design

Retain per-engine demand rather than hiding hotspots in a pool average. Expansion responds to hot engines; contraction checks whether surviving engines can absorb redistributed work. Scaling still depends on routing distributing new work.

KV demand follows actual full-attention, MLA, sliding-window or hybrid allocation. Weight and cache dtype differ: faster FP8 weight computation does not reduce BF16 context residency. Unknown outputs, early EOS and rank partitioning affect estimates. The request layer owns its in-flight ledger, engines own scheduling/cache state, and the controller prepares observations for side-effect-free recommendations.

## Implementation

1. Collect each engine's queued uncached prefill, scheduled/queued decode context and KV capacity with freshness and layout scope.
2. In Foretoken prefill presets, divide queued tokens by context length; custom `load` uses absolute queue tokens. Decode divides scheduled plus queued context by KV capacity; aggregate adds queued prefill.
3. Request one extra Group when any engine meets its expansion threshold. Remove capacity only when all engines meet down thresholds and estimated redistributed pressure remains below the up threshold.
4. Apply controller bounds and lifecycle constraints. Encoder pools or unavailable/incomplete/stale observations hold current capacity within configured bounds.

Foretoken's current observation scope is one full-attention cache group with PCP/DCP=1. llm-d's in-flight producer discounts hits, removes prefill contribution at stream start and request state at stream end/TTL eviction, and clears deleted-endpoint series. Its P/D triggers use summed prefill backlog divided by measured throughput and summed decode KV fractions, respectively. TRE separately calibrates TSS thresholds in a colocated hot-switch prototype.

## Impact and Results

Token and KV signals distinguish a length-mix change at constant request rate and expose rank-local capacity shortages. This reduces the mismatch of request-count scaling, but adding replicas alone cannot fix persistent routing skew. Forecasted output demand can overcount early EOS or miss long tails, while stale detailed state can be less useful than fresh coarse signals. TSS normalization enables calibrated cross-model comparison rather than treating raw tokens/s as equivalent service.

## References

- [Foretoken token-load algorithm](../../../../control-plane/internal/autoscaling/algorithm/decision/token_load.go)
- [Foretoken guide](../../../../docs/autoscaling.md)
- [llm-d in-flight producer](https://github.com/llm-d/llm-d-inference-scheduler/blob/5253f55516f496129248766d1e4b745daba68887/pkg/epp/framework/plugins/requestcontrol/dataproducer/inflightload/producer.go)
- [llm-d P/D prefill trigger](https://github.com/llm-d/llm-d/blob/277b34d062ff954435cf8b1b05efbd75090cf788/guides/workload-autoscaling/keda-epp-token-aware/pd-disaggregation/base/prefill-scaledobject.yaml)
- [llm-d decode trigger](https://github.com/llm-d/llm-d/blob/277b34d062ff954435cf8b1b05efbd75090cf788/guides/workload-autoscaling/keda-epp-token-aware/pd-disaggregation/base/decode-scaledobject.yaml)
- [TRE](https://arxiv.org/html/2609.29160v1)
- [TRE public artifact](https://github.com/zxzx9898/Token-service-share_Rebalancing_Engine)
- [routing and admission](../routing-admission/)
