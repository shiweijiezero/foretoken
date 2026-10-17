<!--
SPDX-License-Identifier: Apache-2.0
SPDX-FileCopyrightText: Copyright contributors to the Foretoken project
-->

# Reuse Non-Prefix Document KV Through Selective Recomputation

English | [简体中文](document-kv-recompute_zh.md)

## Observations and Motivation

Reusing a document's KV after inserting different preceding documents is not ordinary prefix caching. Even identical document tokens have changed causal context. CacheBlend addresses this by reusing most entries while recomputing selected tokens; relocation and semantic correction are separate operations.

## Principles and Analysis

Suppose cached document `B` was processed alone, but a new prompt is `A | B | question`. At layer 0, input embeddings for B can be unchanged apart from positions. At higher layers, B's hidden states should also depend on A. Rotating cached keys to their new positions repairs the positional convention, not that missing dependency.

For a RoPE key `k_p = R(p) k`, shifting by `delta` gives `R(delta) k_p = R(p + delta) k`, under the applicable RoPE convention. Values generally do not receive this rotation. Scaling variants, partial rotary dimensions and MLA require explicit geometry; applying a generic rotation to every cache channel is incorrect.

A useful example is an instruction in A that changes how a sentence in B should be interpreted. Recomputing only the final question may repair question-to-document attention while leaving B's contextual representations stale. Recomputing some B tokens propagates additional information, but does not necessarily reproduce full prefill. Cross-document reasoning and conflicting instructions are consequently more sensitive to stale context than isolated fact retrieval.

## Design

Organize the prompt as separately tokenized document segments, recording each segment's cached and target positions. Match every required rank/read-group object before sparse retrieval, relocate keys under the model's RoPE convention, then let the engine repair context-sensitive states layer by layer. Retrieval and positional correction supply reusable data; selective recomputation supplies the missing cross-document interaction.

Selection uses configured check layers rather than a fixed document-wide repair mask. Tokens with larger key changes receive computation, while their sequence order is preserved for causal attention. The final question and newly introduced tokens still require model execution.

## Implementation

The cited `blender.py` implements the selection and replacement loop; MP `lookup.py`, `retrieve.py` and `rope.py` implement matching, sparse retrieval and relocation. A compact execution is:

1. Assemble segment token IDs and separators; concatenating raw strings can change boundary tokens.
2. Retrieve the layer's cached K/V and apply K-only re-RoPE to moved segments.
3. Compute fresh Q/K/V for the active tokens. On a check layer, calculate `score[t] = sum((K_new[t].float() - K_cached[t].float())**2)`.
4. Keep the ratio-based top-k token indices, sort them into sequence order, and replace their cached K/V.
5. Run selected queries against the complete causal K/V, propagate their updated hidden states, and repeat through later layers.

LMCache's legacy in-process implementation and MP retrieval are distinct paths; the current guide recommends MP. Mooncake transfer, Dynamo tier onboarding and ordinary vLLM prefix caching do not perform this semantic repair.

## Impact and Results

CacheBlend's main evaluation reports 2.2–3.3× lower TTFT than full KV recomputation, with F1/ROUGE-L reductions within 0.02. It uses Mistral-7B, Yi-34B and Llama-70B; the latter two use 8-bit weights. The platform has two A40 GPUs, 128 GB RAM and NVMe storage measured at 4.8 GB/s; the smaller models use one GPU and Llama-70B uses two. The main comparison uses six retrieved 512-token chunks per request across 2WikiMQA, Musique, SAMSum and MultiNews, with a default 15% recomputation ratio.

The saving comes from replacing full document prefill with cache reads and partial computation. Unselected states retain an approximation to the old context, so the repair ratio trades computation against cross-document dependency recovery.

## References

- [CacheBlend method, May 2024](https://arxiv.org/html/2405.16444v1)
- [in-process blender](https://github.com/LMCache/LMCache/blob/4d69cdc43ac9bab9c0c1cd5cac380c480cb32224/lmcache/v1/compute/blend/blender.py)
- [MP module design](https://github.com/LMCache/LMCache/blob/4d69cdc43ac9bab9c0c1cd5cac380c480cb32224/docs/design/v1/multiprocess/modules/blend.md)
- [current legacy guide](https://docs.lmcache.ai/kv_cache_optimizations/blending.html)
