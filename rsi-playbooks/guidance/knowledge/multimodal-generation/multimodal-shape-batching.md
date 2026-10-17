<!--
SPDX-License-Identifier: Apache-2.0
SPDX-FileCopyrightText: Copyright contributors to the Foretoken project
-->

# Reduce multimodal padding and waiting through compatible batching

English | [简体中文](multimodal-shape-batching_zh.md)

## Observations and Motivation

Request count does not express multimodal work. Two images can have different patch counts; two video generations can differ in latent size, duration, guidance, and LoRA. A FIFO queue may leave compatible work unbatched behind an incompatible request. Shape-aware batching aims to reduce padding and idle capacity without changing inputs or starving uncommon shapes.

## Principles and Analysis

For `n` padded rows with token counts `V_i`, illustrative utilization is `U=ΣV_i/(n*max V_i)`. Rows of 256 and 1024 tokens give `U=0.625`. Packing can remove that padding, but sequence boundaries and attention masks must prevent cross-item attention: independent attention scales with `ΣV_i²`, not `(ΣV_i)²`.

Diffusion batching has a different state boundary. Every request owns a latent, solver step, random state, conditioning, and guidance. Matching kernel shapes alone does not make a scheduler or VAE compatible. Reordering the queue can improve batch membership while increasing another request's wait.

## Design

Group requests using the pipeline's actual compatibility rules: resolution and latent shape, guidance mode, output count, LoRA identity and scale, and supported execution mode. Bound waiting independently of device capacity so rare shapes remain dispatchable. Request-level batching executes one complete compatible wave; step batching admits requests between denoising iterations and retains per-request state.

The cited Omni implementation separates different LoRAs or scales, limits Helios to one request, and requires FlashAttention for MiniMax H3 packed batching. Step execution does not support diffusion cache backends at that revision. `max_num_seqs` bounds concurrency but does not implement a step-capable pipeline.

## Implementation

Compatibility-aware batch formation can be expressed as:

```text
anchor = oldest_ready_request(queue)
key = pipeline.compatibility_key(anchor)
batch = [anchor]
scan later ready requests until capacity is reached:
    if pipeline.compatible(key, request): append request
pack supported tensors with item offsets, masks, and positions
execute a full pipeline wave or one supported denoising step
update solver and random state independently for each request
unpack outputs to their original request and input-item positions
retain unfinished requests; release completed request state
```

This organizes work at the scheduler boundary; the packed kernel remains responsible for layout correctness. Use released weights and processors without retraining. Lowering resolution or shortening clips is not a batching improvement. BF16/FP16 alignment, quantized kernels, and graph capture impose backend-specific shape constraints.

## Impact and Results

Packing removes padded elements, while compatibility-aware queue selection reduces singleton waves caused by an incompatible head-of-line neighbor. Neither eliminates waiting for batch formation or an incompatible downstream VAE. The 0.625 example describes padding utilization, not a measured 1.6× service speedup.

Omni's cited MiniMax H3 documentation explicitly reports no measured throughput improvement from its supported packed batching. The path's availability therefore does not establish faster serving. Grouping popular shapes can increase utilization at the expense of rare-shape latency; age-aware dispatch preserves progress. With unchanged inputs and sampler state, scheduling does not intentionally change quality, although numerical ordering and backend execution can affect reproducibility.

## References

- [Qwen2.5-VL, arXiv:2502.13923v1](https://arxiv.org/html/2502.13923v1): dynamic shapes and training sample packing as model background.
- [Omni diffusion execution modes](https://docs.vllm.ai/projects/vllm-omni/en/latest/user_guide/diffusion/execution_modes/) and [pinned source](https://github.com/vllm-project/vllm-omni/blob/37f094a388ba1c79f83c129fda39797c1ebfc2a4/docs/user_guide/diffusion/execution_modes.md): current compatibility and pipeline restrictions; main `37f094a`, released `v0.30.0`.
- [EAServe, arXiv:2609.31551v1](https://arxiv.org/html/2609.31551v1): recent encoder batch timing research; the paper does not establish a specialized heterogeneous-shape packing implementation.
