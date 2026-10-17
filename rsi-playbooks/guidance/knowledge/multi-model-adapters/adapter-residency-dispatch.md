<!--
SPDX-License-Identifier: Apache-2.0
SPDX-FileCopyrightText: Copyright contributors to the Foretoken project
-->

# Reduce adapter thrashing through residency-aware request dispatch

English | [简体中文](adapter-residency-dispatch_zh.md)

## Observations and Motivation

Dynamic loading and LRU eviction do not prevent a residency cliff when demanded adapters outnumber GPU slots. One mandatory fetch can stall shared progress; repeated misses amplify other tenants' tail latency. CLIMB coordinates ingress with the active adapter set so background work does not continually churn residency needed by latency-sensitive VIP traffic.

## Principles and Analysis

With four slots and eight demanded adapters, immediately handing every request to the engine can trigger repeated loads and evictions. Holding inactive background work at ingress reduces exposed fetches during urgent work, but that background queue is still part of end-to-end latency.

Memory cost depends on rank and target modules, not only slot count. For a linear layer of input width `h`, output width `o`, rank `r`, and `b` bytes per weight, raw adapter storage is `b*r*(h+o)`. Rank 128 uses sixteen times the A/B bytes of rank 8 for that layer. A pool padded to maximum rank may allocate still more, and every additional adapter reservation competes with KV capacity.

## Design

CLIMB's `gate_rr` policy maintains an active set and per-adapter queued/in-flight work. It removes idle active entries, activates VIP adapters first by round-robin, then fills remaining capacity with background adapters. The gate counts distinct VIP adapters with queued or in-flight work, not requests and not just resident VIP entries. If that count reaches capacity, background dispatch pauses; in-flight background work is not preemptively erased.

The engine loader remains the device-residency owner. SGLang excludes current-batch and pinned adapters from eviction, copies weights into a slot, clears missing-module buffers, and updates UID mappings consistently across TP ranks. Active work must finish before replacement can reuse its buffers.

## Implementation

The ingress policy is:

```text
remove active entries with no queued or in-flight work
w_vip = count distinct VIP adapters with queued or in-flight work
background_enabled = (w_vip < residency_capacity)
activate queued VIP adapters round-robin until capacity is full
if background_enabled: fill free active slots with background adapters
else: remove idle background entries and prioritize VIP activation
eligible = queued requests for active adapters allowed by the gate
dispatch the oldest eligible request; increment its in-flight count
on completion: decrement in-flight and reconsider active capacity
```

Actual loading and residency confirmation occur outside `gate_rr.py`. CLIMB publishes policy code, curated data, and a GPU rerun wrapper that needs an external serving harness. Its synthetic rank-128 adapters exercise cache pressure, not task quality. Use separately obtained compatible trained LoRAs for task-serving behavior; FP16/BF16 adapter bytes need their own accounting even with a quantized base.

## Impact and Results

CLIMB's ICML publication reports VIP P99 TTFT falling from 38.7 to 13.1 s, while throughput stays at 10.66 requests/s and VIP engine latency remains near 0.13 s. The reported scenario is a heavily utilized single GPU with more active adapters than its remaining residency slots; the publication page does not specify the GPU/base model. Its synthetic rank-128 artifact targets this pressure. The gate reduces background-induced switching when distinct urgent-adapter demand reaches device capacity. Warm urgent traffic can continue without every background arrival forcing a fetch. The cost is background waiting and potentially unused capacity during adapter transitions; ingress wait is not eliminated by better engine-only tails.

Existing SGLang overlap loading can hide copies but also restrict multi-adapter prefill batching. Pinning reduces evictions at the expense of flexible slots. Larger pools reduce misses until KV pressure limits concurrent context. CLIMB's policy changes dispatch order, not adapter semantics; synthetic-weight results concern residency and queueing, not model-quality gains. Unused prefetches can waste bandwidth or evict useful state, while host staging and PCIe determine how much a miss actually stalls execution.

## References

- [CLIMB, ICML 2026](https://proceedings.mlr.press/v306/zhang26dg.html): publication mechanism and artifact link. [Artifact](https://github.com/maxsignalll/CLIMB_artifact/tree/a96f7f3cb65b9bb86dec3d0a1f0c511705f05360) README and `policies/gate_rr.py` at `a96f7f3`; GPU reruns require an external serving harness; synthetic weights exercise residency pressure.
- [SGLang memory pool](https://github.com/sgl-project/sglang/blob/e9400c5a8f3a0d0312a37aecef694f5edfd69341/python/sglang/srt/lora/mem_pool.py): at `e9400c5`; [runtime documentation](https://docs.sglang.io/advanced_features/lora.html).
- [vLLM LoRA](https://docs.vllm.ai/en/latest/features/lora/) and [TensorRT-LLM LoRA](https://nvidia.github.io/TensorRT-LLM/advanced/lora.html): current loading/cache documentation; TensorRT-LLM main at `31c1a3f`.
