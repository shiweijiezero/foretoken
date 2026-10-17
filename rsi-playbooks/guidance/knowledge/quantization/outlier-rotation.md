<!--
SPDX-License-Identifier: Apache-2.0
SPDX-FileCopyrightText: Copyright contributors to the Foretoken project
-->

# Reduce quantization outliers through coordinate rotation

English | [简体中文](outlier-rotation_zh.md)

## Observations and Motivation

One large activation coordinate can determine a block's scale and leave ordinary values poorly resolved. Rotation redistributes energy before quantization. Its value depends on preserving the model computation and on whether transforms can be folded offline or must run at every forward.

## Principles and Analysis

For row-vector inputs Y=XWᵀ and orthogonal R, choose X′=XR and W′=WR; then X′W′ᵀ=XWᵀ. Quantization sees different coordinates although the unquantized linear map is unchanged. A normalized Hadamard transform maps [a,0] to [a/√2,a/√2], reducing the maximum while preserving norm.

Global rotation, within-block rotation, and permutation affect 16/32-value scale groups differently. Arbitrary rotations do not commute with SiLU or a gate product, so moving a transform across nonlinearities can change the model.

## Design

Pair each activation transform with the consumer's transformed weights and keep residual branches, biases, normalization, shared experts, and tied weights in consistent coordinates. Fold compatible transforms into weights offline and retain unavoidable online transforms after nonlinear operations. Recalibrate after rotation. SpinQuant supplies concrete rewrites for embeddings, attention/MLP inputs and outputs, and the LM head, including inverse Hadamard down-projection handling; it is not a generic rewrite for every hybrid module.

## Implementation

The core preparation flow is:

1. Collect channel/block activation statistics and choose fixed or learned orthogonal transforms.
2. Transform eligible weights and adjacent model parameters consistently.
3. Insert required online transforms, including those that cannot cross nonlinearities.
4. Recompute quantization scales on the transformed activations/weights and export the supported format.

llm-compressor lists SpinQuant and QuIP; SpinQuant provides learned rotations and ExecuTorch export. vLLM deployment requires both the exported format and runtime-transform support. TORQ constructs R=R_inter⊗R_intra for MXFP4: Givens rotations equalize covariance diagonals across blocks; within-block rotations alternate with scale updates to reduce codeword-occupancy imbalance sum_j(p_j−1/J)². It retains online activation rotations and folds inverses into consumer weights. Its paper implementation is distinct from an established serving integration.

## Impact and Results

Rotation can reduce clipping and improve codebook utilization without changing the unquantized map. It adds offline model transformation/calibration and, where folding is impossible, online work. Small decode batches can spend more on transforms than they save in quantized GEMM; dense transforms can increase repacked weight error or disrupt activation-quantization fusion. TORQ separates inter-block imbalance from intra-block underuse, while SpinQuant provides an existing learned-rotation path. Quality recovery can enable a smaller model footprint even when transform latency increases.

## References

- [SpinQuant source, commit 8f47aa3f00e8662caf1a484153920a07e5281c3a](https://github.com/facebookresearch/SpinQuant/blob/8f47aa3f00e8662caf1a484153920a07e5281c3a/eval_utils/rotation_utils.py), dated 2025-02-14.
- [Current llm-compressor methods](https://github.com/vllm-project/llm-compressor/tree/990fdd2fb41af48f105dbdebd088a96b627701b1).
- [TORQ method, execution diagram, and limitations, v1](https://arxiv.org/abs/2605.19561).

See also [Kernel design](../kernel-design/).
