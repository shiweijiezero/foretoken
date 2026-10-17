<!--
SPDX-License-Identifier: Apache-2.0
SPDX-FileCopyrightText: Copyright contributors to the Foretoken project
-->

# Accelerate LoRA inference with compact tiles and split-K

English | [简体中文](lora-tiles-split-k_zh.md)

## Observations and Motivation

Shared-base multi-LoRA batching is already established by Punica and S-LoRA, but its low-rank updates can still launch inefficient grids. Unequal packed-QKV output widths create empty expand tiles, and small decode batches expose too little shrink parallelism. SGLang PR #41951, merged as `c0584cf` on 2026-10-13, addresses both with compact expand grids and opt-in split-K shrink.

## Principles and Analysis

For `X=[m,h]`, `W=[o,h]`, and adapter `A_i=[r_i,h]`, `B_i=[o,r_i]`, adapted rows compute `Y=XWᵀ+s_i(XA_iᵀ)B_iᵀ`. Base GEMM serves all rows; shrink produces rank-sized intermediates, and expand adds the appropriate update. Base-only rows receive none.

A rectangular expand grid sized for Q also launches tiles outside narrower K/V slices. Early returns save arithmetic, not launch work. Compacting valid slice tiles removes that overhead. Shrink instead reduces a large input width with small `m` and `r`; splitting the reduction creates more independent programs but requires partial storage and another reduction.

## Design

Use existing CPU-known slice offsets to flatten only valid expand tiles, without copying GPU metadata back to the host. For selected shrink shapes, partition the input-width reduction, accumulate FP32 partials, then reduce live rows and rank columns before casting. This avoids reading uninitialized graph-padding data.

The merged split-K path is disabled by default. `SGLANG_ENABLE_LORA_CSGMV_SPLIT_K=1` enables a narrow automatic policy measured for BF16 rank 32, widths 4096 or 12288, and specific tile parameters. Deterministic inference retains the original path. These dispatch conditions are not a universal low-rank rule.

## Implementation

One adapted layer follows this sequence:

```text
Y = base_GEMM(X, W)
group live rows by adapter and compute valid slice tile offsets
for each active adapter group:
    if opt_in_split_K_policy(shape, dtype, deterministic_mode):
        partials = shrink_over_input_partitions(X, A, accumulate=FP32)
        Z = reduce_live_partials(partials).cast(input_dtype)
    else: Z = direct_shrink(X, A)
    launch only valid expand tiles for the group's output slices
    Y[group_rows] += scale * expand(Z, B)
return Y
```

Use trained PEFT-style adapters compatible with the base checkpoint, modules, rank, and scaling; the kernel change needs no retraining. Tensor parallelism changes local widths. The contributor reports H200 validation. The cited vLLM shrink accepts FP16/BF16 inputs and matching adapter weights, with FP32 partials for batch-invariant execution; quantized base support does not imply quantized adapters.

## Impact and Results

Compact grids reduce launched work for ragged projections; they add no new model approximation. Split-K increases parallelism for small-batch shrink but adds scratch traffic and a reduction launch, so larger prefill batches can favor direct shrink. The shape-specific opt-in policy confines it to its measured operating region.

Split-K changes summation order rather than the low-rank formula. Deterministic execution stays on the original path, while ordinary inference uses the selected numerical tolerance rather than assuming bitwise equality. A faster adapter kernel only shortens its fraction of layer time: base GEMM, routing, metadata, and queueing remain. PR #41951 is a merged implementation, not an unmerged proposal or proof of an end-to-end speedup for every model.

## References

- [SGLang PR #41951](https://github.com/sgl-project/sglang/pull/41951): merged 2026-10-13 at `c0584cf9afefdb4a3004a57af8b1aec501d44a4a`, head `a7deef700821501c37fd1de266d4f1f42848d23a`. Merged [shrink](https://github.com/sgl-project/sglang/blob/c0584cf9afefdb4a3004a57af8b1aec501d44a4a/python/sglang/kernels/ops/lora/dense/chunked_sgmv_shrink.py) and [expand](https://github.com/sgl-project/sglang/blob/c0584cf9afefdb4a3004a57af8b1aec501d44a4a/python/sglang/kernels/ops/lora/dense/chunked_sgmv_expand.py) define the execution paths above.
- [SGLang LoRA documentation](https://docs.sglang.io/advanced_features/lora.html): current Triton/CSGMV, batching, and graph restrictions. Main at `e9400c5`. [PR #42299](https://github.com/sgl-project/sglang/pull/42299) merged 2026-10-03; kernel reorganization.
- [vLLM shrink source](https://github.com/vllm-project/vllm/blob/6849ca696ac9fbecec29a8a8c47f6ee18aea1ee9/vllm/lora/ops/triton_ops/lora_shrink_op.py): grouped metadata and precision path at `6849ca6`.
- [S-LoRA, arXiv:2311.03285v1](https://arxiv.org/html/2311.03285v1): ragged-rank execution and paging.
