<!--
SPDX-License-Identifier: Apache-2.0
SPDX-FileCopyrightText: Copyright contributors to the Foretoken project
-->

# Diagnose simulation mismatch through batch and arrival replay

English | [简体中文](simulation-replay-calibration_zh.md) · [Knowledge](../README.md)

## Observations and Motivation

A simulator can predict kernel durations well yet produce wrong request latency because scheduling, arrivals, or KV release diverge. Near capacity, small timing errors alter future batches and queueing. Calibration must separate runtime-cost mismatch from state-transition mismatch before using predictions to rank serving configurations.

## Principles and Analysis

Vidur models token-level, sequence-level, and communication work with profiled costs and random-forest interpolation. For prefill lengths lᵢ, its equivalent length `√(Σlᵢ²)` preserves quadratic attention work; decode uses aggregate KV reads under its evaluated kernel assumptions. Equal totals can still hide length skew, partition count, dispatch, or expert imbalance.

For a fixed batch sequence, per-iteration error δ can accumulate roughly as kδ. Under arrivals it also changes admission, batch membership, and KV-release time, so end-to-end error is not a constant offset. CPU overhead must follow actual CPU/device dependencies: neither omitting it nor serially summing overlapped work models a pipelined engine.

## Design

Separate two replays. Fixed-batch replay uses actual batch descriptions to compare runtime estimates without scheduling feedback. Arrival replay uses the same workload and policy to expose admission, memory, and completion-order differences. Keep phase, graph mode, dtype, context distribution, kernel, and parallel topology in the calibration domain.

Feature ownership follows the modeled mechanism. MoE needs expert loads; speculative decoding needs acceptance-dependent transitions; hybrid attention needs its own persistent state. More samples cannot repair a simulator path that does not consume these distinctions.

## Implementation

A calibration and event-replay procedure is:

```text
fit operator cost models on measured training shapes
for held-out actual batches b:
    predicted[b] = dependency-aware composition of operator costs
    residual[b] = predicted[b]−measured[b]
    group residuals by phase, shape, context skew and graph mode
initialize request state, KV capacity and an event priority queue
push the recorded arrivals
while events remain:
    advance time to next event
    apply arrival/completion and update queue, request progress, KV
    when engine is idle:
        form a batch using the modeled admission/scheduling policy
        if batch is nonempty: estimate duration; enqueue completion
compare batch membership, KV occupancy and completion order
compare predicted and measured latency percentiles on the same population
```

For percentile q, report `100*(predicted_q−measured_q)/measured_q`, not a percentile of per-request percentage errors. Vidur supplies profiling, estimation, and event scheduling within its published support matrix. Its optional CPU-overhead profiling is implementation-specific. Simulator engine timestamps and Foretoken client chunk timestamps describe different boundaries.

## Impact and Results

Vidur's MLSys 2024 Figures 3–4 report a largest absolute relative discrepancy of 3.33% for static P95 and 8.50% across dynamic median/P95 results. Static latency is execution time without scheduling delay, normalized by output length; dynamic latency is normalized end-to-end time with Poisson arrivals at 85% of measured capacity. The comparison uses each measured percentile as denominator. The setup covers LLaMA2-7B/70B, InternLM-20B and Qwen-72B on Azure VMs with four 80-GB A100 or H100 GPUs connected by pairwise NVLink, using an optimized vLLM fork and Chat-1M, Arxiv-4K and BWB-4K traces. InternLM uses TP2 and the 70B/72B models TP4; the paper does not state numerical precision. These are maxima across the figure comparisons, not raw TTFT/ITL or overload errors.

Fixed-batch residuals identify cost/feature errors; correct durations with different arrival-replay batches identify scheduler or memory semantics. Simulation reduces search cost, while real service execution establishes performance and quality.

## References

- [Vidur, MLSys 2024](https://www.microsoft.com/en-us/research/wp-content/uploads/2024/05/vidur_mlsys24.pdf), §§3–7: operator features, runtime estimator, evaluation population, and near-capacity sensitivity. [arXiv record](https://arxiv.org/abs/2405.05465) identifies the paper and revision.
- [Vidur README](https://github.com/microsoft/vidur/blob/abae7f63aa857300f5cdc6f5e0d27860cd24721b/README.md) and [profiling documentation](https://github.com/microsoft/vidur/blob/abae7f63aa857300f5cdc6f5e0d27860cd24721b/docs/profiling.md): support matrix, canary scope, profiling prerequisites, and CPU-overhead documentation gap.
- [Foretoken timing definitions](../../../../benchmarks/docs/metrics.md): at `e6ae3df6`; simulated engine timestamps must not be equated with client-observed chunk timings.
