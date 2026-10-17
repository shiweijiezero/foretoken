<!--
SPDX-License-Identifier: Apache-2.0
SPDX-FileCopyrightText: Copyright contributors to the Foretoken project
-->

# Reduce MoE Weight Traffic Through Layered Prefill

English | [简体中文](moe-layered-prefill_zh.md)

## Observations and Motivation

Long prompts sharing GPUs with ongoing decode need bounded prefill steps to avoid generation stalls. Token chunking achieves that, but each chunk traverses every layer. In MoE models, even small chunks can collectively activate most experts, repeatedly reading similar GPU-resident weights for few tokens per expert.

## Principles and Analysis

Layered prefill splits the model into contiguous layer groups instead of splitting only the input. A larger input batch visits each group's experts once, amortizing their weight reads over more tokens. It does not remove any required token-layer computation; ongoing decode still reads its selected experts.

The benefit depends on expert overlap between token chunks and weight reuse within expert kernels. Dense models have different access patterns: the authors observed regressions for Qwen3-8B. Relaxed token-gap targets permit larger token chunks and narrow the advantage. Pure decode has no extra prefill weight reads to remove.

## Design

Advance new prefill through one layer group per iteration, while existing decode requests traverse all layers. For four layers and eight input tokens, four-token chunking visits layers 1–4 twice; two-layer grouping processes all eight tokens through layers 1–2, then resumes through layers 3–4.

The new request produces its first token only after all groups complete. Larger groups reduce transitions but lengthen decode interruptions. Very long inputs can use larger token chunks plus layer groups to bound intermediate-state memory without returning to tiny per-expert batches.

## Implementation

1. Track token progress separately from the next layer group; group completion is not full-model prefill completion.
2. Run the selected contiguous layer range for the prefill batch, alongside full-layer decode work.
3. Retain the group's output hidden states, required residuals, positions and KV mappings. Completed layers keep their KV; later layers await execution.
4. Resume the retained state at the next group. Mark prefill complete and sample only after the final group.
5. Include retained state in cancellation, preemption, regrouping and graph lifetimes.

The public prototype uses Nano-vLLM. The cited vLLM scheduler output has token counts but no layer-group progress; its Qwen3 MoE forward does not resume individual requests by group. Pipeline-parallel layer partitioning alone does not implement this schedule.

## Impact and Results

On two H100 80GB GPUs with NVLink and TP2, the paper reports Qwen3-30B-A3B expert-weight traffic decreasing from 28.5 TB to 25.1 TB on ShareGPT (12%) and from 35.6 TB to 21.7 TB on arXiv (39%), relative to token-chunked prefill in mixed serving. These are memory-traffic reductions, not serving-speed percentages. Retaining larger prefill batches adds intermediate-state memory and cross-iteration state management.

## References

- [From Tokens to Layers: Redefining Stall-Free Scheduling for MoE Serving with Layered Prefill, v2](https://arxiv.org/html/2510.08055v2): scheduling mechanism and MoE serving results.
- [Layered Prefill implementation and reproduction instructions](https://github.com/scale-snu/layered-prefill/tree/053f80e5201a7c0ab56e468e3d578907a2ca9cc3), including dependencies, a FlashAttention patch, serving, and measurement commands.
- [current scheduler output](https://github.com/vllm-project/vllm/blob/6849ca696ac9fbecec29a8a8c47f6ee18aea1ee9/vllm/v1/core/sched/output.py)
- [current Qwen3 MoE forward](https://github.com/vllm-project/vllm/blob/6849ca696ac9fbecec29a8a8c47f6ee18aea1ee9/vllm/model_executor/models/qwen3_moe.py)
