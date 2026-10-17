<!--
SPDX-License-Identifier: Apache-2.0
SPDX-FileCopyrightText: Copyright contributors to the Foretoken project
-->

# Size SLO capacity through calibrated service curves

English | [简体中文](slo-capacity-curves_zh.md)

## Observations and Motivation

Peak tokens/s does not establish how many requests a replica can serve while meeting TTFT and token-spacing objectives. Capacity changes with lengths, cache reuse, batching and target percentile. Sizing only decode throughput leaves prefill queues and first-token deadlines unprotected.

## Principles and Analysis

For an arrival rate λ in requests/s, define `μ_SLO = sup{λ: required latency and success constraints hold}` for the chosen workload. A first replica target is `ceil(λ_demand/μ_SLO)`, not a guarantee under imbalance, cache fragmentation or startup delay. In split serving, P/D capacities are separate and transfer consumes first-token budget.

The approximate queueing model uses `T(i)=α+iδ`: i is active batch occupancy, α fixed iteration overhead and δ marginal time per active request, including compute/KV access. Its birth–death state counts active plus queued requests; beyond batch limit B, only B are active. State-dependent service predicts occupancy, waiting and mean TTFT/ITL.

Poisson arrivals and mean lengths make this a compact mean model, not a tail-latency predictor. Memory can limit capacity before compute does; cache dtype, hybrid groups, TP duplication and long outputs change that boundary independently of weight GEMM speed.

## Design

Associate each service curve with the model, device, weight/cache dtype, runtime and parallelism configuration. Keep latency percentiles and success outcomes alongside throughput. Dynamo already combines engine-capacity models with cache-hit and decode acceptance-length inputs, holding capacity when its model is not ready.

Latency feedback is another design: llm-d uses `max(P90_TTFT/S_TTFT, P90_TPOT/S_TPOT)`, where S_TTFT and S_TPOT are the respective latency targets. Its rules prefer predicted histograms, fall back to measured ones, compute P90 over a one-minute rate window, cap pressure at 2, then smooth for 45 seconds. The cited guide covers one variant/one SLO pair rather than split-stage estimation.

## Implementation

1. Build a single-replica service curve from arrival rate and request mix, preserving offered arrivals and completion/rejection outcomes.
2. Calibrate iteration costs and memory feasibility, or fit α and δ and solve the state-dependent queueing model for mean occupancy and delays.
3. Find the largest rate satisfying the declared latency statistics and success constraints; do not replace a percentile target with its mean.
4. Divide offered demand by that qualified capacity and round up; for P/D, calculate each stage and account for transfer.
5. Return a recommendation through the existing decision boundary. The controller applies bounds, stabilization and pending-capacity constraints and owns actuation.

Foretoken's current queue/token policies do not implement this calibrated estimator; its recommendation/controller separation supplies the ownership boundary. llm-d's feedback path drives KEDA using a dead band and pending-capacity credit.

## Impact and Results

The queueing paper evaluates H100/vLLM with BF16 Llama-3.1-8B and Qwen2.5-14B, batch limit 256 and token budget 8192. Across 112 retained light/moderate-load points per model, mean relative ITL errors are 4.6%/7.9% and TTFT errors 13.6%/15.8%, respectively. Near-saturation runs were excluded. These quantify mean prediction accuracy, not serving speedups or tail accuracy. Calibrated sizing expresses latency-qualified capacity; tighter response can cost more replicas rather than faster inference.

## References

- [An Approximate Queueing Model](https://arxiv.org/html/2609.20957v1)
- [Dynamo planner README](https://github.com/ai-dynamo/dynamo/blob/b2078c3127bd2ad3a70a6d39beeb06ff2d0a67fd/components/src/dynamo/planner/README.md)
- [Dynamo throughput code](https://github.com/ai-dynamo/dynamo/blob/b2078c3127bd2ad3a70a6d39beeb06ff2d0a67fd/components/src/dynamo/planner/core/throughput_scaling.py)
- [llm-d SLO guide](https://github.com/llm-d/llm-d/blob/277b34d062ff954435cf8b1b05efbd75090cf788/guides/workload-autoscaling/slo-aware/README.md)
- [llm-d recording rules](https://github.com/llm-d/llm-d/blob/277b34d062ff954435cf8b1b05efbd75090cf788/guides/workload-autoscaling/slo-aware/prometheus-rule.yaml)
- [Foretoken autoscaling guide](../../../../docs/autoscaling.md)
- [Foretoken token-load decision](../../../../control-plane/internal/autoscaling/algorithm/decision/token_load.go)
- [measurement and simulation](../measurement-simulation/)
