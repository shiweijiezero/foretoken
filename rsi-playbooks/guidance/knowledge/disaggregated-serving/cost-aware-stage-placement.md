<!--
SPDX-License-Identifier: Apache-2.0
SPDX-FileCopyrightText: Copyright contributors to the Foretoken project
-->

# Reduce serving cost through profiled heterogeneous stage placement

English | [简体中文](cost-aware-stage-placement_zh.md)

## Observations and Motivation

The accelerator with the highest peak FLOPs is not necessarily the cheapest device for every inference stage. Prefill, decode attention, and MoE experts have different compute, bandwidth, and residency needs. Stage placement should exploit these differences while preserving a supported execution and state-transfer path, rather than independently selecting the cheapest isolated kernel.

## Principles and Analysis

First distinguish feasibility from performance: a stage/device/dtype/parallelism combination must load weights, hold state, execute its operators, and exchange state through a supported connector. For feasible combinations, capacity must be measured under the stage latency budget.

A basic objective is `min Σ_s,h n_s,h*c_h`, with integer worker counts, stage-capacity, memory, transfer, and end-to-end SLO constraints. Cost includes the relevant host and network share. One prefill worker sustaining 8 requests/s and two decode workers sustaining 4 each form an 8-request/s path. Replacing prefill with a cheaper 5-request/s device reduces the whole path to 5, despite unchanged decode capacity. Smaller decode memory can instead reduce residency and violate TPOT.

## Design

Use workload-specific capacity curves, not peak device specifications. Jointly assign hardware, replica counts, and workload slices; count duplicated weights, conversion, staging, topology, and startup. AFD attention benefits from HBM bandwidth and KV capacity, while sufficiently batched experts exploit compute. P/D can similarly use different devices only if KV handoff remains affordable.

MegaScale-Infer maps attention to H20 and experts to L40S. AIBrix's optimizer maps workload signatures to throughput bins and its Melange solver minimizes GPU cost with integer replicas and assignment constraints. Dynamo sizes P/D capacities separately; llm-d Kueue borrowing handles quotas, not stage execution modeling or KV migration.

## Implementation

A small profiled allocation search is:

```text
enumerate supported stage/device/dtype/parallelism combinations
read SLO-feasible capacity curves and physical memory requirements
construct candidate integer replica counts within the fleet budget
assign workload slices to candidates with adequate stage capacity
check link payload demand, topology, and compatible state handoff
include weights, workspace, staging, and host/network cost
reject allocations violating end-to-end latency or memory constraints
choose the lowest-cost feasible complete request path
materialize each role and route only after its model is ready
```

This summarizes a stage-aware optimization, not an existing all-backend switch. Foretoken's E/P/D example permits per-stage resources and engine arguments. Keep model and state representations explicit: BF16 MegaScale-Infer and FP8 FastAFD use different operating points; cross-vendor execution needs supported kernels and connectors, not just matching tensor dimensions.

## Impact and Results

MegaScale-Infer reports H20-attention/L40S-expert decoding throughput per purchase-cost unit up to 3.24× versus H20 vLLM and 1.86× versus H20 TensorRT-LLM. Including prefill, throughput per cost improves up to 1.66×. Its study uses BF16 weights, activations, and KV for Mixtral-8×22B, DBRX, and a scaled MoE, with a 150 ms inter-token target and production-derived traffic whose median prompt/output lengths are 571/159 tokens. Normalized purchase prices are historical inputs, not current cloud rental prices.

These are system-level results combining stage placement and AFD execution. FastAFD's physical evidence is FP8 MoE on GB200 NVL72; future Rubin/LPX projections are not measured heterogeneous deployment. More TP ranks, underfilled experts, transfers, power limits, and startup can offset a nominal device-price advantage.

## References

- [MegaScale-Infer](https://arxiv.org/html/2504.02263)
- [FastAFD](https://haoailab.com/blogs/fastafd/)
- [optimizer](https://github.com/vllm-project/aibrix/blob/d0500b0399cf35ad0bf95d073faa83c23308f463/python/aibrix/aibrix/gpu_optimizer/optimizer/optimizer.py)
- [Melange solver](https://github.com/vllm-project/aibrix/blob/d0500b0399cf35ad0bf95d073faa83c23308f463/python/aibrix/aibrix/gpu_optimizer/optimizer/solver/melange/solver.py)
- [planner](https://github.com/ai-dynamo/dynamo/blob/b2078c3127bd2ad3a70a6d39beeb06ff2d0a67fd/components/src/dynamo/planner/core/throughput_scaling.py)
- [Kueue rebalancing guide](https://github.com/llm-d/llm-d/blob/277b34d062ff954435cf8b1b05efbd75090cf788/guides/workload-autoscaling/kueue-rebalancing/README.md)
- [E/P/D example](../../../../examples/encoder-prefill-decode/README.md)
- [heterogeneous hardware](../heterogeneous-hardware/)
