<!--
SPDX-License-Identifier: Apache-2.0
SPDX-FileCopyrightText: Copyright contributors to the Foretoken project
-->

# Balance speculative work through adaptive verification budgets

English | [简体中文](adaptive-verify-budget_zh.md)

## Observations and Motivation

A fixed draft length spends more work per iteration to reduce the number of target decode iterations. Under concurrent requests or long contexts, that extra verification can consume capacity even when acceptance stays unchanged. The objective is committed output per occupied service time, not acceptance percentage.

## Principles and Analysis

Draft generation and verification are separate decisions. Stopping an autoregressive drafter saves later forwards; truncating a one-pass drafter saves target work but cannot recover the completed draft forward. For conditional acceptance estimates a[r,j], prefix survival is s[r,j] = product(a[r,1..j]). An approximate chain objective is:

```text
sum_r (1 + sum_j<=K[r] s[r,j])
/ (draft_time + verify_time(admitted_slots, contexts) + correction_commit_time)
```

K[r] must describe a contiguous prefix. The extra one accounts for a correction or bonus token, away from EOS and output limits. High confidence at a late position is useless if its predecessors are unlikely to survive.

## Design

Use the existing target/drafter pair. Estimate survival per position and select admitted prefix lengths against the batch step-cost curve. The target evaluates only those prefixes; commit accepted outputs plus correction/bonus and discard both rejected and unverified proposals. Rebuild the next draft from committed history. Graph padding and context-dependent verification cost belong in the allocation cost, because a smaller logical budget need not produce a smaller executed graph.

## Implementation

The pinned vLLM implementation uses a trained DSpark confidence head where available, or an online logistic estimator fitted to verification outcomes. Its core allocation flow is:

1. Generate proposals and predict conditional acceptance.
2. Form prefix-survival estimates and select lengths using the profiled step-cost curve.
3. Pack admitted queries and run target verification.
4. Apply exact rejection sampling, commit valid outputs, and update the estimator from reached positions.

The snapshot supports EAGLE, MTP, DFlash, DSpark, and standalone draft models, requires full CUDA graphs and compatible device-decided query lengths, and excludes LoRA and pipeline parallelism. Startup profiling defaults to an 8192-token synthetic context. The separate `num_speculative_tokens_per_batch_size` schedule can reduce K to zero but excludes data parallelism; it is not per-request device-side allocation.

## Impact and Results

Allocation reduces target rows spent on low-survival suffixes and favors requests with useful continuations. It adds estimation, packing, and state bookkeeping. A stale context-cost curve or poorly calibrated survival estimates can allocate work badly, while graph rounding can erase the row savings. The cited RFC reports low-load null results and motivates saturation-aware allocation; it does not establish a universal speedup. State recovery for recurrent models follows the committed prefix, as described in [replay buffers](../attention-state/state-write-replay.md).

## References

- [Pinned vLLM adaptive verification guide](https://github.com/vllm-project/vllm/blob/6849ca696ac9fbecec29a8a8c47f6ee18aea1ee9/docs/features/speculative_decoding/adaptive_verification.md) and [dynamic schedule guide](https://github.com/vllm-project/vllm/blob/6849ca696ac9fbecec29a8a8c47f6ee18aea1ee9/docs/features/speculative_decoding/dynamic_speculative_decoding.md) define this snapshot's support boundary.
- [PR #47808](https://github.com/vllm-project/vllm/pull/47808), confidence-scheduled verification, merged 2026-08-12 as `7f7a32cfec0f1bc5b73c37200b86631523a1ea8f`.
- [RFC #48202](https://github.com/vllm-project/vllm/issues/48202): contributor-reported low-load null results and saturation-sensitive motivation. Its linked [PR #48692](https://github.com/vllm-project/vllm/pull/48692) is closed without merge; current support is described in the pinned guide above.
