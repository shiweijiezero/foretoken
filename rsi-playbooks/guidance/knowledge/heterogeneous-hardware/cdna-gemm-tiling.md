<!--
SPDX-License-Identifier: Apache-2.0
SPDX-FileCopyrightText: Copyright contributors to the Foretoken project
-->

# Improve CDNA GEMM: Shape-Aware Tiles and Layouts

English | [简体中文](cdna-gemm-tiling_zh.md)

## Observations and Motivation

A GEMM tuned for large prefill can waste most rows during decode or in lightly loaded experts. AMD CDNA needs shape-aware matrix instructions, wavefront layouts, and local data share (LDS) staging rather than a launch configuration copied from a CUDA GPU. Total MoE batch size is not the token count of each expert GEMM.

## Principles and Analysis

For X[M,K]W[K,N], work is 2MKN. Minimum logical traffic reads X and W once and writes Y once; tile reloads, scales and spills add traffic. Small M limits weight reuse, while large M raises arithmetic intensity. Larger tiles improve reuse but consume registers/LDS and can reduce occupancy.

CDNA3 gfx942 MI300X/MI325X has 64-thread wavefronts and 64 KiB LDS; CDNA4 gfx950 MI350X/MI355X retains that wavefront width with 160 KiB LDS in AMD's specifications. Instruction layouts remain dtype/architecture-specific. As an explanatory shape example, M=4 wastes most rows of an M=128 tile. Split-K creates more work groups but adds partial-result storage and reduction.

## Design

Choose a supported library path by architecture, dtype, M/N/K, and quantization layout. AMD's guide supplies AITER linear/skinny-GEMM and hipBLASLt choices, FP8 padding, and CDNA4 FP4 assembly GEMM. Native CDNA4 MXFP4 requires MI350X/MI355X and matching weights; vLLM's gfx942 groupwise-int4 conversion is a different representation.

Map each operand tile to the selected MFMA instruction’s lanes. Weight packing and scale layout are prepared for that consumer, not changed independently. For MoE, group tokens by expert and select schedules from per-expert row counts.

## Implementation

A tiled GEMM computes:

```text
assign each work group an output tile (m0,n0)
initialize FP32 or supported accumulation registers
for k0 over K in BK-sized blocks:
    load guarded X[BM,BK] and W[BK,BN] from HBM
    stage in the instruction-compatible LDS/register layout
    synchronize producer/consumer access before tile reuse
    issue MFMA fragments with required block scales; accumulate
apply any remaining row/channel scales and bias/activation epilogue
store only valid M,N elements
```

Double buffering overlaps the next load with current arithmetic when register/LDS budgets permit. Split-K uses disjoint K ranges and a separate or supported fused reduction before the epilogue. Preserve dtype accumulation and quantization scale semantics, including padded rows. AITER's GEMM/MoE operators and tuning configurations implement the owning schedules; `op_tests/test_gemm_a8w8.py` and `test_moe.py` expose actual input layouts. BF16, FP16, FP8 and MXFP4 require separate dispatch choices.

## Impact and Results

Shape-aware tiles reduce padded arithmetic and improve weight reuse or available work-group parallelism. Compatible operand/scale layouts remove conversion traffic at the consumer boundary. Larger tiles can instead cause spills or lower occupancy; split-K increases reduction traffic. Extra CDNA4 LDS expands staging options; the chosen tile still determines register pressure and occupancy. Tiny decode, large prefill, and skewed expert loads exercise different trade-offs.

## References

- [AMD architecture specifications](https://rocm.docs.amd.com/en/latest/reference/gpu-arch-specs.html).
- [Current vLLM/AITER optimization guide](https://rocm.docs.amd.com/projects/ai-ecosystem/en/latest/optimization/vllm-v1-optimization.html).
- [AITER at the cited commit](https://github.com/ROCm/aiter/tree/c09dc31787c21cd7fa7acbe4addef256766bd58a).
- [vLLM MXFP4 layout and conversion](https://github.com/vllm-project/vllm/blob/6849ca696ac9fbecec29a8a8c47f6ee18aea1ee9/vllm/model_executor/layers/quantization/mxfp4.py).

 · [Kernel design](../kernel-design/) · [Knowledge index](../README.md)
