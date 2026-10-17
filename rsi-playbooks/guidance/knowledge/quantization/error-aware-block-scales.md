<!--
SPDX-License-Identifier: Apache-2.0
SPDX-FileCopyrightText: Copyright contributors to the Foretoken project
-->

# Reduce quantization error with activation-aware block scales

English | [简体中文](error-aware-block-scales_zh.md)

## Observations and Motivation

Two blocks with equal weight reconstruction error can produce different model errors because the workload uses their input directions differently. Maximum-based scales ignore that distinction. Activation-aware calibration changes scale selection while retaining ordinary NVFP4 inference representation.

## Principles and Analysis

For Y=WX, W[N,K], and calibration inputs X[K,T], define Δ=Q(W,s)−W. The output reconstruction objective is:

```text
E = ||ΔX||²_F = tr(ΔHΔᵀ),  H = XXᵀ
```

H is the uncentered input second moment, often called a local Hessian here. Its diagonal weights frequently excited channels; off-diagonal entries capture reinforcement or cancellation of errors on correlated inputs. Fixed normalization does not change the optimum. NVFP4's uneven E2M1 codebook also makes maximum-based scaling imperfect: Four Over Six compares placing a block maximum near 4 versus 6 without changing the four-bit payload.

## Design

ModelOpt's Local-Hessian partitions the input axis into 16-element blocks, collects a 16×16 second-moment matrix per block, and searches representable FP8 scales using Δ_b H_b Δ_bᵀ. The documented sweep contains 126 positive nonzero candidates. Independent blocks keep the search manageable but omit cross-block error interactions. Layerwise calibration may feed later layers earlier fake-quantized outputs, capturing propagation rather than assuming every input remains BF16.

## Implementation

The offline algorithm is:

1. Collect calibration activations for each eligible dense or MoE linear layer.
2. Accumulate H_b = X_b X_bᵀ for each input block.
3. For each representable scale, quantize/reconstruct its weights and score Δ_b H_b Δ_bᵀ.
4. Keep the minimizing scale and export standard NVFP4 weights and metadata.

Four Over Six instead reconstructs the max-to-4 and max-to-6 alternatives and selects by reconstruction error; its repository defaults to MSE and offers Blackwell CUDA/Triton plus a PyTorch reference. The same 4/6 choice cannot generally transfer to MXFP4: a 1.5× scale change is not representable by power-of-two E8M0. ModelOpt provides Local-Hessian in its existing calibration path. Deployment needs the exported format's normal kernel, not a new Local-Hessian kernel.

## Impact and Results

Activation-aware scoring reduces errors in directions represented by calibration rather than treating all weight errors equally. Scale search adds offline statistics and reconstruction work, not runtime operators or a new checkpoint type. Four Over Six redistributes precision within the floating-point codebook. Neither method repairs missing calibration evidence: rare MoE experts or shifted domains can have weak statistics. Block-local scoring also leaves correlated errors between blocks unmodeled. Quality can improve with unchanged GEMM execution; scale candidate counts are algorithm parameters, not speed measurements.

## References

ModelOpt snapshot `b5f10f8bf96201241c35b86498557bd6fdde10ca`.

- [Local-Hessian method, API, and limitations](https://nvidia.github.io/Model-Optimizer/announcements/local-hessian.html).
- [Calibration implementation](https://github.com/NVIDIA/Model-Optimizer/blob/b5f10f8bf96201241c35b86498557bd6fdde10ca/modelopt/torch/quantization/model_calib.py).
- [Four Over Six paper, arXiv 2512.02010v5](https://arxiv.org/abs/2512.02010) and [author implementation](https://github.com/mit-han-lab/fouroversix). Author implementation at `dadfad6901d473a734fe71e0b082e70ee993e23a` (2026-04-21).
