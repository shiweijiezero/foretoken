<!--
SPDX-License-Identifier: Apache-2.0
SPDX-FileCopyrightText: Copyright contributors to the Foretoken project
-->

# Reduce recurrent state writes with replay buffers

English | [简体中文](state-write-replay_zh.md)

## Observations and Motivation

Recurrent decode reads and writes a full matrix per request and layer every step. Speculative verification can add intermediate snapshots for rejection recovery. At large batches, this traffic can dominate despite a history-independent state size. ReplaySSM represents the logical state as a checkpoint plus recent records, avoiding full-state materialization at every position.

## Principles and Analysis

For value-first state `S[V,K]`, columns `v_t,d_t` of length `V`, columns `k_t,q_t` of length `K`, and scalar GDN gates `alpha_t,beta_t`:

```text
d_t = beta_t * (v_t - alpha_t*S_previous*k_t)
S_t = alpha_t*S_previous + d_t*transpose(k_t)
o_t = S_t*q_t
S_h = (product_i alpha_i)*S0
    + sum_j (product_{i>j} alpha_i)*d_j*transpose(k_j)
```

The query includes output scaling. Compute `S_h*q` from the checkpoint and low-rank records without writing `S_h` to HBM. A flush after `L` committed records amortizes full-state writes; checkpoint reads remain. Because each GDN correction depends on previous state, a verify chunk needs a coupled triangular transformation, not independent draft updates.

## Design

Keep a checkpoint, a circular record buffer, and an explicit committed cursor per request slot. Verifying three drafts but committing two includes only the first two records in the next logical state; the third can be overwritten. Accepted-length conventions determine whether the bonus token is included.

Separate a high-precision recurrence checkpoint from a lower-precision readout copy. Retain raw `v_t` inputs so flush can recompute coupled corrections from the high-precision checkpoint, then cast the resulting state independently into both copies. This prevents low-precision readout corrections from becoming the next permanent checkpoint, though output rounding remains.

## Implementation

1. Append current inputs or draft-chain inputs to the request's replay window; compute corrections and outputs against the checkpoint plus preceding valid records.
2. Advance the committed cursor only through accepted tokens. Exclude rejected records before any folding operation.
3. At the flush boundary, reconstruct from the high-precision checkpoint and committed raw inputs, replace both checkpoint copies, and recycle folded records.
4. For prefix export or transfer, materialize the logical state or transfer checkpoint and committed records together. Reset cursor and buffer ownership when a slot is reused.

SGLang merged ordinary decode in PR #28451 and speculative integration in #28695. The pinned `gdn_replayssm_spec_decode.py`, GDN backend, and state pool implement serving cursors and linear-chain verification. The separate compact-cache PR #27658 is not this merged path. KDA channel gates require a different replay derivation.

## Impact and Results

Replay removes repeated full-state writes and speculative snapshots, exchanging them for buffer reads, reconstruction, and periodic flushes. Increasing `L` saves more writes but increases replay work; forced prefix publication can flush an incomplete window. Large, bandwidth-bound recurrent batches are the intended regime, while batch-one launch or compute costs can dominate. In a hybrid model, the end-to-end benefit is bounded by recurrent-layer time: unchanged MoE and global attention remain. Precision separation limits feedback into permanent state, not numerical changes in readout.

## References

- [ReplaySSM v1](https://arxiv.org/html/2610.13778v1), [submission record](https://arxiv.org/abs/2610.13778): submitted 2026-10-09; recurrence, replay, speculation, and precision separation.
- [SGLang RFC #28511](https://github.com/sgl-project/sglang/issues/28511): closed; integration scope and model-dependent reported results.
- [PR #28451](https://github.com/sgl-project/sglang/pull/28451): merged at `a10a24e9a7fdc0ddcc7dc3e90c0d4983707db028`; [PR #28695](https://github.com/sgl-project/sglang/pull/28695): merged at `c41c573ce9ed2053d38aa711582e19a67a5e9755`; [PR #27658](https://github.com/sgl-project/sglang/pull/27658): open, not used as evidence of shipped compact-cache support.
- [Actual verify kernel at `e9400c5a8f3a0d0312a37aecef694f5edfd69341`](https://github.com/sgl-project/sglang/blob/e9400c5a8f3a0d0312a37aecef694f5edfd69341/python/sglang/kernels/ops/attention/fla/gdn_replayssm_spec_decode.py): circular replay, folding, commit conventions, and precision paths.
