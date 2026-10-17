<!--
SPDX-License-Identifier: Apache-2.0
SPDX-FileCopyrightText: Copyright contributors to the Foretoken project
-->

# Reduce prefill/decode interference through conditional stage separation

English | [简体中文](prefill-decode-separation_zh.md)

## Observations and Motivation

A long prefill can delay every decode request sharing an engine. Separating prefill (P) and decode (D) isolates that interference and permits different batching or parallelism, but adds queues, KV transfer, and duplicate weights. Conditional separation uses actual uncached work and decode headroom to decide when isolation outweighs those additions.

## Principles and Analysis

Approximate first-token paths are `T_aggregate=Q_A+P_A+first_decode_A` and `T_split=Q_P+P_P+Q_X+transfer+Q_D+first_decode_D`. Routing, cache reservation, and batching couple the terms. Splitting pays when removed interference exceeds added queueing and handoff.

A 32K-token prompt with 31K reusable tokens on the selected D needs only 1K local prefill. Remote P may save little compute while transferring substantial state. A cold 32K prompt can instead monopolize a mixed engine. Cache state belongs to the actual destination, not to an abstract global hit rate.

Role counts are a slower decision: initial SLO-calibrated sizing is `n_P=ceil(λ/μ_P)`, `n_D=ceil(λ/μ_D)`. Lengths, hits, and occupancy change both capacities.

## Design

Keep conditional per-request routing, replica scaling, and live role conversion separate. The first two do not implement worker conversion. A routing decision uses effective input length and load, with decode-busy override where the engine provides it. The handoff owns state retention until transfer completion and selects compatible D capacity.

Dynamo's cited conditional path supports vLLM and TensorRT-LLM, not SGLang. Its planner sizes roles independently and accounts for readiness/pending starts. llm-d's prefix-based decider uses `if-available` and can signal HTTP 412 for remote prefill; Production Stack also provides orchestrated P/D selection. Foretoken's workflow selects fresh D after P completes and retains cross-stage cleanup.

## Implementation

A conditional routing sequence is:

```text
observe compatible decode candidates, cache state, and load
effective_input = input_tokens - destination_reusable_prefix
estimate local prefill interference and remote handoff cost
if effective work warrants splitting or decode-busy override applies:
    select ready P and execute remote prefill
    retain source KV through handoff
    select compatible D and transfer required KV
    begin decode after destination state is ready
else: execute local prefill and decode on the selected aggregate path
retire owned state after completion or confirmed cancellation
```

This is the decision structure, not a universal length threshold. The engine's supported weight/KV dtype, TP layout, and connector constrain the handoff. Dense/GQA BF16 behavior differs from MoE or hybrid states. FP8 weights do not establish FP8 KV transfer, and host-staged TCP adds copies.

## Impact and Results

Separation removes interference between long prefill and ongoing decode, while conditional routing avoids transfer for highly reusable prompts. It does not remove decode saturation or transport queueing. Short inputs, low concurrency, large local prefix hits, or slow staging can favor an aggregate engine with chunked prefill.

GPU accounting includes all P and D workers, duplicated model residency, startup, and retirement; a faster D-only trace is not proof of cheaper complete serving. The 32K/31K example explains effective work, not measured latency. Independent role sizing follows changing workload demand instead of assuming a fixed P:D ratio. Stale cache/load observations can still choose a path whose predicted advantage disappears before execution.

## References

- [conditional disaggregation guide](https://github.com/ai-dynamo/dynamo/blob/b2078c3127bd2ad3a70a6d39beeb06ff2d0a67fd/docs/fern/pages/developer-guide/advanced-customizations/conditional-disaggregation.md)
- [throughput planner](https://github.com/ai-dynamo/dynamo/blob/b2078c3127bd2ad3a70a6d39beeb06ff2d0a67fd/components/src/dynamo/planner/core/throughput_scaling.py)
- [prefix-based decider](https://github.com/llm-d/llm-d-inference-scheduler/blob/5253f55516f496129248766d1e4b745daba68887/pkg/epp/framework/plugins/scheduling/profilehandler/disagg/prefix_based_pd_decider.go)
- [routing code](https://github.com/vllm-project/production-stack/blob/4e076e8de3698a3729b575e3a5ade65ffbb929cf/src/vllm_router/routers/routing_logic.py)
- [workflow](../../../../data-plane/frontend/src/server/src/runtime/workflow.rs)
- [autoscaling owner](../../../../control-plane/internal/autoscaling/README.md)
