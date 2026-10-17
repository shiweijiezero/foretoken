<!--
SPDX-License-Identifier: Apache-2.0
SPDX-FileCopyrightText: Copyright contributors to the Foretoken project
-->

# Prevent pipeline backlogs through joint stage-capacity allocation

English | [简体中文](pipeline-capacity-balance_zh.md)

## Observations and Motivation

Adding encoder or prefill workers can increase backlog when they produce state faster than transfer and decode can consume it. Multistage serving balances computation, admission, transport, and intermediate-state retention. Faster isolated stages are useful only if the complete path can absorb their output within the latency and memory budgets.

## Principles and Analysis

For `E→P→X→D`, where X is KV transfer, a first stability condition is `λ<min(n_E μ_E,n_P μ_P,μ_X,n_D μ_D)`. Capacities must use one workload distribution and comparable request units; raw encoder items/s and text tokens/s are not interchangeable. Decode capacity includes request lifetime and growing contexts.

At arrivals of 8 requests/s and E/P/X/D capacities of 12/10/7/9, doubling P cannot exceed the 7-request/s transfer bottleneck. X accumulates work while arrivals remain at 8. Little's law gives mean resident requests `λW` in a stable stage; resident bytes require size weighting. A minority of long prompts can dominate memory.

## Design

Jointly allocate stage replicas, link capacity, and HBM against an end-to-end tail budget. Stage completion is a readiness barrier, not atomic reservation of all future stages. Early D reservation protects handoff but can strand slots during E/P; late D selection avoids that reservation but encounters downstream contention.

SARA models P as M/G/k, transfer as M/G/1, and continuous-batched D with a birth–death approximation. Its search allocates compute, bandwidth, and HBM under quantile constraints. Poisson arrivals and independent lengths are model assumptions, not guarantees for correlated multimodal bursts. Dynamo documents E/PD and E/P/D with NIXL embeddings; backend support differs.

## Implementation

A capacity plan and its execution are:

```text
derive stage-time and state-byte distributions for the same workload
enumerate E/P/D counts, transfer capacity, and memory assignments
estimate stage queues and end-to-end latency quantiles
select a feasible allocation within the total resource budget
admit request work at a rate the complete path can sustain
execute E; publish completed embedding descriptors
execute P; retain its state through KV handoff
select compatible D and start only after required state is ready
on completion or cancellation: retire intermediate resources
```

The algorithm describes joint allocation rather than claiming atomic reservations. Foretoken's maintained Qwen2.5-VL-3B example uses shared encoder storage, Mooncake P→D transport, and separately configurable Pools. Its workflow selects D after P and retains cleanup across E/P barriers; autoscaling evaluates role capacities while committing a complete serving cohort.

## Impact and Results

Balancing stages avoids producing unusable work that queues as embeddings or KV. In the 12/10/7/9 example, expanding P changes neither sustainable rate nor X overload; improving transfer or limiting admitted demand addresses the actual bottleneck. Intermediate state can outlive compute and become the limiting physical resource.

SARA combines modeling/simulation with A100/SGLang measurements. In its physical LLaMA 3.1 8B trace replay, it reports peak-period maximum goodput degradations relative to SARA of 21.5% for Splitwise, 45.6% for Colocated, and 57.2% for empirical allocation. These are the baselines' losses relative to SARA, not the percentage by which SARA improves each baseline. LLaMA 3.1 8B, Qwen2.5 32B, and GPT-3 175B configurations in the study should not all be described as physical runs with one common dtype. Separation adds weight replicas, buffers, startup, and failure domains. Mean capacities alone cannot bound tails from correlated large-image/long-prompt bursts, and extra reservation can improve handoff predictability while reducing utilization.

## References

- [encoder-disaggregation guide](https://github.com/ai-dynamo/dynamo/blob/b2078c3127bd2ad3a70a6d39beeb06ff2d0a67fd/docs/fern/pages/use-cases/multimodal-serving/encoder-disaggregation.md)
- [SARA paper](https://arxiv.org/html/2609.26763v1)
- [maintained E/P/D example](../../../../examples/feature/encoder-prefill-decode/README.md)
- [runtime workflow](../../../../data-plane/frontend/src/server/src/runtime/workflow.rs)
- [autoscaling owner](../../../../control-plane/internal/autoscaling/README.md)
- [multimodal generation](../multimodal-generation/)
