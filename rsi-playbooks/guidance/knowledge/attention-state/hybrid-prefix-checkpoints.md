<!--
SPDX-License-Identifier: Apache-2.0
SPDX-FileCopyrightText: Copyright contributors to the Foretoken project
-->

# Reuse hybrid prefixes with aligned state checkpoints

English | [简体中文](hybrid-prefix-checkpoints_zh.md)

## Observations and Motivation

A cached attention prefix is insufficient to resume a hybrid model: recurrent matrices and convolution buffers must represent exactly the same processed tokens. The previous request's final state cannot resume a continuation that diverged earlier. Aligned checkpoints make this shared boundary reusable without retaining a recurrent snapshot after every token.

## Principles and Analysis

Suppose requests share tokens 0–89. Attention KV covers 90 tokens, but recurrent checkpoints exist only after 64 and 128. The state after 128 already includes a different suffix; resume from 64 and replay tokens 64–89 instead.

Usable cache coverage is the intersection across attention groups, recurrent layers, and convolution state. With checkpoint interval `C`, a usable preceding checkpoint can require up to `C-1` replayed tokens. Smaller intervals reduce recomputation but add snapshots and copying. Fixed-size decode state does not imply cheap snapshots.

Physical allocation adds another constraint: vLLM groups compatible layers into equal-size pages. A large recurrent matrix can enlarge attention blocks or add padding. Physical page bytes and logical token intervals therefore have different effects on capacity and hit granularity.

## Design

Publish immutable snapshots tagged by processed-token boundary. Lookup finds a boundary all required groups can supply; each continuation gets writable recurrent state through copy-on-write. Retain attention coverage for the suffix replay, then resume all groups at the same boundary.

Publication, transfer, and writable state are separate lifecycle stages. A connector must move convolution and recurrent state as well as attention pages, and complete those transfers before use. The cache manager owns this coordination; router affinity alone cannot create missing state.

## Implementation

1. Match the request prefix against each cache group and find a mutually resumable checkpoint boundary.
2. Acquire attention pages and the corresponding recurrent/convolution snapshots. Complete pending transfers and copy-on-write before mutation.
3. Replay any tokens between the checkpoint and the shared-prefix end, updating every layer consistently; continue with the divergent suffix.
4. At eligible scheduler boundaries, publish reusable snapshots and retain or evict their state blocks through the existing manager.

The pinned `MambaManager` implements aligned lookup, internal checkpoints, partial-tail caching, and pending copies. vLLM v0.30.0 fine-grained matching requires `mamba-cache-mode=align`, EAGLE/MTP on the Mamba group, and an explicit `prefix-match-unit` smaller than the Mamba block size and dividing applicable block sizes. Its documentation excludes multi-module MTP. These prerequisites describe that version's finer matching mode, not all hybrid prefix reuse.

## Impact and Results

Aligned reuse avoids recomputing the whole shared prefix; in the 90-token example, the valid 64-token snapshot leaves 26 tokens to replay. This is boundary arithmetic, not a measured latency result. Finer checkpoints save replay at the cost of snapshot memory, copies, and potentially less KV capacity. Repeated system prompts and multi-turn continuations offer reusable boundaries; unrelated traffic mostly consumes retention capacity. Copy-on-write preserves independent divergent continuations, while the common-boundary rule prevents an attention-only hit from resuming the wrong recurrent history.

## References

- [vLLM v0.30.0 automatic prefix caching](https://docs.vllm.ai/en/v0.30.0/features/automatic_prefix_caching/): fine-grained checkpoint option and exact documented prerequisites.
- [vLLM hybrid cache design](https://docs.vllm.ai/en/latest/design/hybrid_kv_cache_manager.html): shared pages, group allocation, prefix intersection, and documented experimental limits.
- [`MambaManager` at `6849ca696ac9fbecec29a8a8c47f6ee18aea1ee9`](https://github.com/vllm-project/vllm/blob/6849ca696ac9fbecec29a8a8c47f6ee18aea1ee9/vllm/v1/core/single_type_kv_cache_manager.py): implemented aligned/fine-grained lookup, checkpoint retention, and copy-on-write lifecycle.
