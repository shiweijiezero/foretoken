<!--
SPDX-License-Identifier: Apache-2.0
SPDX-FileCopyrightText: Copyright contributors to the Foretoken project
-->

# Reduce Launch Overhead: Backend-Aware Graphs and Streams

English | [简体中文](backend-graph-replay_zh.md)

## Observations and Motivation

Small decode kernels can spend substantial time in repeated host submission. Graph replay reduces those launches, while auxiliary streams overlap independent work. CUDA, ROCm, and MACA expose similar interfaces but differ in capture support, communicator ordering, stream binding, and workspace lifetime. A warmup that succeeds does not establish safe repeated replay.

## Principles and Analysis

For n kernels, repeated launch work contributes roughly n·t_launch. Capture reduces submission work, not arithmetic. Replaying captured size G for actual token count M adds G−M padded rows and retains graph buffers.

As an explanatory example, nine tokens replaying a sixteen-token graph add seven rows; their cost must be weighed against avoided launches. Speculative decode has several query tokens per request, so request count is not the full shape key.

If main produces inputs, aux computes a shared expert, and main combines outputs, ordering requires main→aux before reads and aux→main before consumption. Every allocation must remain valid across these uses and later replays.

## Design

Use the engine's existing graph dispatcher with compatible batch descriptions, attention backend, and capture modes. vLLM separates compilation from capture and supports full/piecewise execution: `FULL_DECODE_ONLY` excludes prefill; `FULL_AND_PIECEWISE` combines full uniform decode with piecewise execution elsewhere. Unsupported keys run eagerly.

Keep stable graph-owned inputs/workspaces and explicit dependencies for auxiliary streams. Capture participation and collective order remain consistent across ranks, including mixed eager/graph paths. NCCL's documented multiple-communicator graph caveat is library-specific, not an RCCL/MCCL guarantee.

## Implementation

The dispatch and stream procedure is:

1. Select a supported capture bucket from actual query-token count and batch/backend requirements. Allocate persistent inputs, outputs and workspace for that graph instance.
2. Warm the owning operators and communicator paths, then capture their supported operations with matching rank participation. Preserve internal-stream event dependencies.
3. Copy new batch values into stable input buffers, initialize padding and masks as required by the model, then replay. If no compatible key exists, use the dispatcher’s eager or piecewise path.
4. For auxiliary work, record input-ready on main; aux waits, computes and records output-ready; main waits before combining. Do not reuse shared workspace until every accessing stream completes.
5. Retain capture buffers until graph retirement, and consume only real batch outputs.

AMD's vLLM guide specifies graph choices by attention backend. MetaX uses its plugin and matching native libraries; the cited MACA all-gather goes through the base process-group path. That actual path, rather than a direct wrapper, determines capture compatibility.

## Impact and Results

Replay removes repeated host launch work and can improve short-step cadence. Correct stream dependencies expose independent work without stale reads. Padding and retained graph memory add computation and reduce memory headroom; an uncovered eager segment can still dominate. Global synchronization after each operation would serialize the overlap this design preserves. Graph support and progress depend on the actual backend/operator/communicator combination.

## References

- [vLLM graph design at the cited commit](https://github.com/vllm-project/vllm/blob/6849ca696ac9fbecec29a8a8c47f6ee18aea1ee9/docs/design/cuda_graphs.md).
- [NCCL graph contracts](https://docs.nvidia.com/deeplearning/nccl/user-guide/docs/usage/cudagraph.html).
- [AMD graph choices](https://rocm.docs.amd.com/projects/ai-ecosystem/en/latest/optimization/vllm-v1-optimization.html).
- [MetaX communicator implementation](https://github.com/MetaX-MACA/vLLM-metax/blob/cec91763dbe968fe4478fc066f57ecf5877c3965/vllm_metax/distributed/device_communicators/cuda_communicator.py).

 · [Compiler/runtime](../compiler-runtime/) · [Knowledge index](../README.md)
