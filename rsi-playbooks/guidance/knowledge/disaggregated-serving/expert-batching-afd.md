<!--
SPDX-License-Identifier: Apache-2.0
SPDX-FileCopyrightText: Copyright contributors to the Foretoken project
-->

# Increase expert batch utilization through attention–FFN disaggregation

English | [简体中文](expert-batching-afd_zh.md)

## Observations and Motivation

Sparse MoE decode gives each expert only a fraction of the batch, while every colocated attention replica stores expert weights beside request-local KV. Attention–FFN disaggregation (AFD) aggregates tokens from several attention workers into larger expert batches and removes expert weights from attention-side memory. The objective is better expert weight amortization and more KV residency.

## Principles and Analysis

With `B` tokens, top-k routing, and `E` balanced experts, each expert sees approximately `B*k/E` tokens. At `B=256`, `k=8`, `E=128`, that is 16; aggregating four batches gives 64. Skew still makes the busiest expert a bottleneck.

Every MoE layer adds attention→dispatch→FFN→combine dependencies. Microbatches overlap attention for one batch with expert work and communication for another. The critical path depends on attention/expert balance, dispatch/combine, and synchronization. Increasing microbatch count can hide waiting while shrinking GEMMs and adding padding; throughput is not the serial time simply divided by the number of batches.

## Design

FastAFD's coordinator produces aligned attention-group and expert-group plans. Dummy batches preserve collective order on inactive attention lanes, and a common dispatch bucket keeps communication/graph shapes compatible. Attention workers own KV pages and split prefill by token work, decode by request count. Expert workers run event-coordinated dispatch→expert→combine or a distinct persistent MegaMoE path.

Completion retires KV pages and emits outputs only for real requests. FastAFD is an independent `minisgl` runtime whose cited scheduler requires naive KV caching. Foretoken's E/P/D workflow orchestrates request stages, not these per-layer exchanges. The design-space paper's vLLM PR #29772 is closed without a merge commit.

## Implementation

One coordinated iteration is:

```text
coordinator selects real batches and common dispatch capacity
insert dummy batches for attention lanes with no real work
attention workers allocate KV and form ordered microbatches
for each layer and microbatch in the shared plan:
    run attention with request-local KV
    dispatch routed activations to expert workers
    execute grouped expert FFNs while other attention work proceeds
    combine expert outputs and signal completion before dependent use
retire completed real requests and their pages; ignore dummy outputs
```

The nonpersistent path uses CUDA events and graph-compatible shapes; persistent MegaMoE uses its own execution path. Required assets are compatible MoE weights, not newly trained models. FastAFD evaluates FP8 Qwen3-235B-A22B and MiniMax-M2.5 on GB200 NVL72; BF16 MegaScale-Infer is a separate configuration.

## Impact and Results

FastAFD reports 1.35–1.45× per-GPU steady-state decode throughput over tuned colocated vLLM on one GB200 NVLink/NVSwitch domain. Qwen3-235B gives 1.41× at 8K context and 1.44× at 16K; MiniMax-M2.5 gives 1.45× and 1.35×. The FP8 runs use two microbatches and KV utilization near 0.85. All attention and FFN GPUs count in the denominator, including expert GPUs hosting no requests. Measurement begins after prefill and excludes online-arrival effects.

Much of the benefit comes from larger attention-side KV capacity, not just faster expert kernels. MegaScale-Infer's three/four-microbatch choices depend on its different balance and communication costs. The B200 design-space study combines measured backend costs with AstraSim network modeling; its strict-SLO frontiers are modeled, not NVL72 service measurements. Per-layer communication, skew, dummy padding, and underfilled expert batches remain costs.

## References

- [FastAFD's technical report](https://haoailab.com/blogs/fastafd/)
- [MegaScale-Infer paper](https://arxiv.org/html/2504.02263)
- [design-space paper](https://arxiv.org/html/2605.28302v1)
- [coordinator](https://github.com/hao-ai-lab/FastAFD/blob/3c7161949310b6d59d6b4cf9bf997a4935c8113b/python/minisgl/afd_coordinator.py)
- [scheduler](https://github.com/hao-ai-lab/FastAFD/blob/3c7161949310b6d59d6b4cf9bf997a4935c8113b/python/minisgl/afd_scheduler.py)
- [expert worker](https://github.com/hao-ai-lab/FastAFD/blob/3c7161949310b6d59d6b4cf9bf997a4935c8113b/python/minisgl/afd_expert_worker.py)
- [vLLM PR #29772](https://github.com/vllm-project/vllm/pull/29772)
- [E/P/D workflow](../../../../data-plane/frontend/src/server/src/runtime/workflow.rs)
- [autoscaling and placement](../autoscaling-placement/)
