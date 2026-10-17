<!--
SPDX-License-Identifier: Apache-2.0
SPDX-FileCopyrightText: Copyright contributors to the Foretoken project
-->

# Reduce TP Synchronization Through Shape-Aware Collective Selection

English | [简体中文](tp-collective-selection_zh.md)

## Observations and Motivation

Tensor parallelism (TP) splits weights to fit or accelerate a model, but introduces repeated synchronization inside each layer. Small decode messages may be latency-bound; large prefill messages may be bandwidth-bound. Increasing TP can shrink local matrix multiplies while increasing the fraction spent on collectives.

## Principles and Analysis

A column-parallel projection computes disjoint output-feature slices. A subsequent row-parallel projection computes partial sums over input-feature slices. To produce replicated outputs, ranks sum those partials with all-reduce. Alternatively, when the consumer can retain a token shard, reduce-scatter produces only that shard, with a later all-gather where full rows are required. This changes tensor ownership between stages; replacing an operation without changing its consumer is incorrect.

For `T` tokens, hidden width `H`, and element size `s`, one replicated activation contains `B=T*H*s` bytes. An ideal ring all-reduce communicates approximately `2*(p-1)/p*B` bytes per rank for `p` ranks. This is a bandwidth model, not a latency prediction: launch, synchronization, topology, and protocol matter, especially when `T=1`.

## Design

The engine selects communication at the producer-consumer boundary, where output shape and replicated or sharded ownership are known. Reduction dtype and batch-invariant numerical behavior constrain which algorithms can be substituted.

Use message shape and the actual peer/multicast domain to select a supported path. Legacy IPC all-reduce is not MNNVL; ordinary inter-node InfiniBand does not provide NVLink multicast. Keep frequent TP communication in a fast domain when possible. Bucketing can combine independently ready reductions, not reductions separated by dependent nonlinear computation.

## Implementation

The cited custom-collective selection follows these decisions:

1. Determine required all-reduce/all-gather/reduce-scatter from the producer and consumer's tensor ownership.
2. Check dtype, byte alignment, contiguity, group size, architecture and reachable peers/multicast storage for that operation.
3. Legacy custom all-reduce rejects more than eight ranks even though the enclosing object supports additional sizes for other operations. In eager execution, copy into registered buffers; captured execution can use registered graph addresses.
4. For eligible MNNVL reduce-scatter, select Lamport, multimem or another communicator according to architecture/size and initialized multicast storage. Preserve the batch-invariant path where algorithm switching is disabled.
5. Execute the chosen collective and retain its buffers until completion before the dependent layer consumes results.

`custom_all_reduce.py` owns the legacy selector; MNNVL all-gather/reduce-scatter and low-SM multimem are separate source paths. FP16/BF16, FP32 and quantized GEMM outputs do not imply identical reduction accumulation.

## Impact and Results

Shape-aware selection avoids unsupported or expensive communication paths at the actual message size. Registered graph addresses can remove eager staging copies as well as reduce launch overhead. Reduce-scatter reduces replicated output storage only when downstream ownership is changed consistently.

Higher TP increases weight capacity and reduces local GEMM size, but adds coordination and can make one-token decode latency-bound. Larger buckets reduce launches while delaying the first consumer and extending buffer lifetimes. PP exchanges some TP synchronization for stage dependencies and bubbles, so the largest TP degree is not inherently the lowest-latency configuration.

## References

- [custom collective source at 6849ca69](https://github.com/vllm-project/vllm/blob/6849ca696ac9fbecec29a8a8c47f6ee18aea1ee9/vllm/distributed/device_communicators/custom_all_reduce.py)
- [parallelism deployment documentation](https://github.com/vllm-project/vllm/blob/6849ca696ac9fbecec29a8a8c47f6ee18aea1ee9/docs/serving/parallelism_scaling.md)
- [Pipeline microbatch balance](pipeline-microbatch-balance.md)
