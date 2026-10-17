<!--
SPDX-License-Identifier: Apache-2.0
SPDX-FileCopyrightText: Copyright contributors to the Foretoken project
-->

# Reduce Collective Delays: Backend-Specific Algorithms and Compute Overlap

English | [简体中文](collective-compute-overlap_zh.md)

## Observations and Motivation

Tensor/expert parallelism can move the bottleneck from GEMM to communication. The fastest isolated all-reduce may slow the complete stage by consuming compute resources or waiting for a late rank. NCCL, RCCL and MCCL have analogous interfaces but distinct topology, kernels, transport, and graph behavior.

## Principles and Analysis

A rough model is `T=α·steps+bytes/β`. Ring all-reduce moves about `2(p−1)/p` times the payload size per rank, with step count growing with p. Small decode messages tend toward latency limits; large prefill messages emphasize bandwidth. As an explanatory size example, BF16 width 4096 gives 8192 bytes per token and 8 MiB for 1024 tokens; actual parallel layouts alter message counts.

Independent overlap can approach `max(T_compute,T_comm)+T_unoverlapped`, but shared SM/HBM pressure can lengthen both. A second stream alone does not create independent work, and unequal expert loads can leave the slowest rank dominant.

## Design

Keep collectives with the engine's communicator owner and preserve operation order per communicator. Overlap only computation independent of the collective result, such as an independent branch or a supported next tile. Consumer operations wait on device completion, not host enqueue completion.

Algorithm/channel choice follows actual message sizes and topology through the supported backend. AMD documents Quick Reduce and ROCm all-gather/reduce-scatter expert paths; reduced-bit reductions additionally require quantization/dequantization and acceptable error. MetaX's plugin selects all-to-all managers and routes MACA all-gather through its base process group.

## Implementation

A stage with independent branch B executes:

```text
main stream computes and packs payload A
record A-ready on main
communication stream waits for A-ready
enqueue collective in identical communicator order on all ranks
main computes B, which does not depend on collective output
communication stream records collective-complete
main waits for collective-complete before combine(A_result, B)
reuse payload/workspace only after the last consumer completes
```

For EP, preserve routed token order, expert counts, padding, and inverse routing through dispatch → expert GEMM → combine. Pipeline chunks only through a supported owner that preserves the same collective sequence across ranks, including ranks with no local tokens. Reduced-bit paths pack quantized values and scales before transfer and reconstruct the supported reduction result afterwards. Use NCCL on CUDA, RCCL on ROCm, and the plugin's MCCL/process-group paths on MACA; their graph settings are not interchangeable. Current RCCL source lives in `rocm-systems/projects/rccl`.

## Impact and Results

Overlap removes serial waiting for independent work; compatible packing avoids extra layout copies. Message-aware backend selection addresses latency-dominated versus bandwidth-dominated cases. Gains can disappear when communication slows GEMM, a late rank controls completion, or packing/quantization dominates. Low-bit transfer reduces bytes but changes numerical behavior. A communication-bandwidth improvement therefore does not automatically shorten the complete serving stage.

## References

- [Current ROCm systems source](https://github.com/ROCm/rocm-systems/tree/f1b316f12c75430787032e99405834f59a026ae1/projects/rccl).
- [AMD communication and Quick Reduce guidance](https://rocm.docs.amd.com/projects/ai-ecosystem/en/latest/optimization/vllm-v1-optimization.html).
- [MetaX communicator and all-to-all selection](https://github.com/MetaX-MACA/vLLM-metax/blob/cec91763dbe968fe4478fc066f57ecf5877c3965/vllm_metax/distributed/device_communicators/cuda_communicator.py).
- [NCCL graph and rank-participation contracts](https://docs.nvidia.com/deeplearning/nccl/user-guide/docs/usage/cudagraph.html).

 · [Parallelism/communication](../parallelism-communication/) · [Knowledge index](../README.md)
