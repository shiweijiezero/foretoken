<!--
SPDX-License-Identifier: Apache-2.0
SPDX-FileCopyrightText: Copyright contributors to the Foretoken project
-->

# Balance Capacity and Latency Through CPU/GPU Placement

English | [简体中文](cpu-gpu-offload_zh.md)

## Observations and Motivation

Additional memory can make a model runnable without making it fast. CPU-resident layers, sequential GPU layer partitions, tensor shards, and per-forward weight transfers pay different costs. Equal-memory placement can leave a fast device waiting for a slower one, while weight offload can add link traffic to every generated token.

## Principles and Analysis

Device j must fit `weights+state+workspace+graph_memory≤usable_memory`. Sequential layer latency is approximately partition compute plus activation transfers; a tensor-parallel layer follows the slowest shard plus communication. Memory-proportional placement establishes capacity, not balanced execution.

For S weight bytes crossing a link each token at sustained bandwidth B, transfer alone is at least S/B. As an explanatory dimensional example, 4 GiB at 32 GiB/s takes 125 ms before compute/synchronization. CPU-resident execution instead reads weights from CPU memory, while a layer boundary transfers activations rather than all weights. These are different offload mechanisms.

## Design

Use contiguous layer placement to reduce boundaries when capacity is the main goal; retain each layer's KV/state with its execution owner. A supported tensor split exposes intra-layer parallelism but repeats reductions and requires compatible architecture, kernels and shard alignment.

llama.cpp provides GPU-layer counts and split proportions. Its experimental tensor mode requires supported architectures and flash attention, excludes quantized cache types in the cited guide, and has no automatic fitting. Its performance guidance focuses on CUDA; HIP/RCCL defaults differ. vLLM CPU execution uses architecture-appropriate dtype/ISA and NUMA-local threads and memory.

## Implementation

For sequential placement:

1. Account for weights, target-context KV/state, workspace and graph buffers per device. Assign contiguous supported layers within each memory budget.
2. Load or convert each layer's weights once into its owning backend representation. Initialize CPU-resident allocations on the rank's NUMA node and reserve host cores for serving.
3. Execute a partition, transfer its activation to the next partition, wait on the copy-completion event, then execute the next. Return only required final results.

For streaming weight offload, allocate device staging buffers and pinned host storage; enqueue the next layer's weight copy on a transfer stream while the current layer computes. The compute stream waits for weight-ready; the transfer stream waits for last-use before overwriting a staging slot. Link bandwidth, staging capacity and actual independent work limit overlap.

A tensor split instead partitions each supported operator's weights/state, executes local shards and performs its required collectives per layer. It is not interchangeable with the sequential algorithm. Mixed devices require compatible backend paths; a CPU matrix ISA does not itself supply GPU offload.

## Impact and Results

Layer placement and CPU residency expand feasible model/context capacity. Contiguous partitions reduce activation boundaries; prefetched weight transfers can hide some link work. Costs remain CPU bandwidth, CPU matrix execution, link transfer, synchronization, and slow-shard waiting. Quantized weights reduce bytes only if conversion and kernels support the destination representation. A configuration that enables a larger model at higher latency is a capacity gain, not a speedup.

## References

- [llama.cpp multi-GPU execution at commit 77c4540](https://github.com/ggml-org/llama.cpp/blob/77c4540ce4667f4577753fe034a3a8042f510b8f/docs/multi-gpu.md).
- [Current vLLM CPU ISA, dtype, NUMA, and threading guidance](https://docs.vllm.ai/en/latest/getting_started/installation/cpu/).
- [llama.cpp token-generation performance entry point](https://github.com/ggml-org/llama.cpp/blob/77c4540ce4667f4577753fe034a3a8042f510b8f/docs/development/token_generation_performance_tips.md); its example measurements are historical, not current hardware comparisons.

 · [Parallelism/communication](../parallelism-communication/) · [Knowledge index](../README.md)
