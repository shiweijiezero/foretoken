<!--
SPDX-License-Identifier: Apache-2.0
SPDX-FileCopyrightText: Copyright contributors to the Foretoken project
-->

# Hide MoE Communication Through Microbatch and Block-Level Overlap

English | [简体中文](moe-dispatch-overlap_zh.md)

## Observations and Motivation

MoE dispatch and combine can dominate decode when each rank has few tokens. Asynchronous APIs alone do not remove the dependency between dispatch and expert GEMM, or between combine and the next layer. Useful overlap requires independent work or finer producer-completion information.

## Principles and Analysis

Overlapping work shortens the critical path only when the overlapped computation is independent of the transfer. Dispatch must produce valid expert rows before GEMM consumes them; combine must finish before the next layer uses its result. Whole-kernel completion exposes less overlap than per-block completion. Resource contention can lengthen both communication and compute, so their overlapped duration is not simply the maximum of isolated timings.

## Design

Create independent work with two legal microbatches, or expose fine-grained readiness within one batch. Dual-batch overlap (DBO) overlaps one microbatch's communication with the other's attention/expert computation. Single-batch overlap (SBO) sends completed expert-row blocks before the whole GEMM ends, requiring a cooperating producer.

Streams and completion handles preserve dispatch→GEMM→combine dependencies. Communication still consumes SMs and competes for HBM/shared-memory resources; waiting consumers must leave resources for their producer. A single ordinary decode request does not supply two useful microbatches unless another execution mode expands its work.

## Implementation

On DeepEP V2.5 main:

1. Launch asynchronous dispatch on the communication stream; run independent work and wait only before expert inputs are needed.
2. Consume expanded rows, routing metadata and the handle. With CPU synchronization disabled, use GPU receive counts rather than treating allocated capacity as valid rows.
3. Compute expert outputs and apply router weights at the documented compute boundary; main combine sums the supplied outputs.
4. Launch combine, overlap other work and wait before the next dependent consumer reads the result.

vLLM DBO makes all DP ranks agree on a split, alternates the two microbatches' dispatch/compute/combine, and records eligible schedules in full CUDA graphs. An empty second microbatch can disable splitting. In `antgroup-opt` SBO, GEMM publishes per-block completion signals; combine-send acquire-waits for a threshold, sends that block, and publishes expert completion after all blocks finish.

The cited NVIDIA V2.5 path requires Hopper+, CUDA 13.1+, PyTorch 2.10+ and NCCL 2.32.3+, with intra-node NVLink/inter-node RDMA. Dispatch accepts BF16 or scaled FP8; combine takes BF16. V1/NVSHMEM branches use different APIs.

## Impact and Results

DBO hides communication behind genuinely independent computation, while SBO shortens the combine tail by releasing completed blocks early. Both retain synchronization and signaling costs. If overlap slows GEMM through resource contention, a larger overlap region can still produce a longer layer.

DeepEP V2.5 main explicitly uses GPU SMs for EP dispatch/combine and does not support zero-SM RDMA EP. SBO PR #483 was merged into `antgroup-opt`, not V2 main; #453 remains unmerged in the cited state. Batch eligibility and version-specific integration determine which mechanism actually executes.

## References

- [main 93eb6eb2](https://github.com/deepseek-ai/DeepEP/tree/93eb6eb238127e96c6d7a4a625a6dad158348509)
- [DBO design at 6849ca69](https://github.com/vllm-project/vllm/blob/6849ca696ac9fbecec29a8a8c47f6ee18aea1ee9/docs/design/dbo.md)
- [PR #483](https://github.com/deepseek-ai/DeepEP/pull/483)
- [PR #453](https://github.com/deepseek-ai/DeepEP/pull/453)
