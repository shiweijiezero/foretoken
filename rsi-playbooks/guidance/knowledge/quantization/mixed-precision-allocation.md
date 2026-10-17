<!--
SPDX-License-Identifier: Apache-2.0
SPDX-FileCopyrightText: Copyright contributors to the Foretoken project
-->

# Balance mixed-precision quality, memory, and kernel cost

English | [简体中文](mixed-precision-allocation_zh.md)

## Observations and Motivation

Uniform low-bit precision gives equally sized layers the same storage budget even when their quality sensitivity differs. Mixed precision can retain quality within a memory budget, but average bits alone do not predict latency or whether fused operators can execute the assignment.

## Principles and Analysis

ModelOpt AutoQuantize estimates candidate operator sensitivity using a diagonal-Fisher approximation:

```text
S_i,f ∝ sum_k g_i,k² (Y_i,k − Y^Q_i,f,k)²
min sum_i S_i,f(i), subject to sum_i N_i b_i,f(i) <= N_total b_target
```

Y is the reference output, Y^Q the fake-quantized output, and g the scoring-loss gradient with respect to Y. Calibration observations supply scores. N_i counts eligible weights; b_i,f models format bits. Excluded embeddings/norms still consume memory outside this budget. Local sensitivities and diagonal curvature approximate, rather than fully represent, cross-layer error interactions.

## Design

Select one deployable format per operator group, preserving fused QKV, gate/up, and expert quantization contracts. Aggregate member sensitivities and costs and solve the constrained integer assignment. A format menu must match exporter and runtime support; independently choosing Q, K, and V can break fused projection execution. Effective bits model storage, not stage latency, and native FP8 can outperform weight-only four-bit execution at large M.

## Implementation

The existing search flow is:

1. Capture calibration inputs and scoring-loss output gradients.
2. Replay each supported candidate quantizer locally and accumulate sensitivity.
3. Form deployment groups and their effective-bit costs.
4. Solve one-format-per-group assignment under the requested budget.
5. Quantize/export the selected assignment and load through the existing runtime format path.

The cited ModelOpt snapshot documents gradient and KL scoring. Supported BF16, FP8, NVFP4, and MXFP4 choices depend on hardware, model exclusions, and MoE layouts. There is no need for a parallel checkpoint schema. Activation conversion, padding, epilogues, and transitions remain part of each group's actual execution, even when absent from the bit objective.

## Impact and Results

Sensitivity-aware allocation protects expensive-to-quantize groups while spending fewer bits elsewhere. It adds calibration forwards/backwards, candidate replay, and assignment search. Mixed formats can change scratch memory, graph capture, and feasible batch size, so service costs are not simply additive. A smaller assignment can improve throughput by admitting larger batches while worsening low-load latency. The established objective optimizes quality under effective-bit constraints; it does not claim a measured globally latency-optimal service.

## References

- [AutoQuantize scoring, search, grouping, and limitations](https://nvidia.github.io/Model-Optimizer/announcements/autoquantize.html), ModelOpt snapshot `b5f10f8bf96201241c35b86498557bd6fdde10ca`.
- [vLLM executable ModelOpt formats](https://docs.vllm.ai/en/latest/features/quantization/modelopt/).
- [Current SGLang quantization backends](https://docs.sglang.io/advanced_features/quantization.html).

See also [Heterogeneous execution](../heterogeneous-hardware/).
