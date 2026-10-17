<!--
SPDX-License-Identifier: Apache-2.0
SPDX-FileCopyrightText: Copyright contributors to the Foretoken project
-->

# Reuse graph memory through lifetime-aware pooling

English | [简体中文](graph-lifetime-pooling_zh.md)

## Observations and Motivation

Separate captures can retain separate buffers, leaving less memory for KV. Sharing those buffers without tracking live tensors can overwrite outputs needed by another branch or model step. The allocator must preserve captured addresses while reclaiming storage whose values are no longer needed.

## Principles and Analysis

CUDA Graph replay uses captured pointers. Values at those addresses can change, but moving an allocation does not update the recorded pointers. Replay also launches kernels without repeating ordinary allocator operations.

Consider `A → B → D` and `A → C → D`. After replaying A, its output remains live. Recording C must protect that output even though replay did not update allocator accounting. D can require a second recording because its input address differs between the B and C paths. Shape, execution path, and lifetime all affect reuse.

## Design

PyTorch CUDA Graph Trees share one graph-aware pool across compatible recordings. The tree records which outputs remain live and the allocation state at path boundaries. A new branch restores that accounting before allocating; a known branch replays only when its address and lifetime conditions match.

Stable parameters, previous graph outputs, and ordinary eager inputs are treated differently. Eager inputs can require a copy into static staging. Functions mutating such inputs are skipped because mutation of the staging copy would not update the original tensor.

## Implementation

The pool lifecycle can be summarized as:

```text
for the next compiled region:
    identify its shape, path, and live input addresses
    if a compatible recording exists:
        stage inputs that need stable addresses
        replay it
    else:
        restore allocator accounting for live predecessor outputs
        warm and record the new region
    retain output storage until its consumers release it
```

Parameters/buffers and previous graph outputs can support mutation after stable-address checks. Cross-iteration outputs need explicit lifetime handling: PyTorch supplies step markers and cloning strategies; v2.14 also documents opt-in cloning of live user-visible output storage. Persistent recurrent state and KV remain engine-owned rather than being reclaimed as temporary outputs.

## Impact and Results

Shared pooling reduces duplicated capture storage, potentially leaving more capacity for request state. Lifetime tracking prevents a newly recorded branch from overwriting a still-live predecessor output.

The trade-off is additional bookkeeping and, for retained outputs or unstable inputs, copying. More shapes and branches can still require more recordings. Cloning a complete recurrent state every token may remove a reuse hazard but adds substantial memory traffic, so state ownership is central to the result.

## References

- [PyTorch v2.14 CUDA Graph Trees](https://github.com/pytorch/pytorch/blob/v2.14.0/docs/source/user_guide/torch_compiler/torch.compiler_cudagraph_trees.md): pool checkpoints, mutation cases, and output lifetime handling.
- [vLLM v0.31.0 CUDA Graph design](https://docs.vllm.ai/en/v0.31.0/design/cuda_graphs/): engine wrappers and dispatch.
- [Graph buckets](launch-graph-buckets.md) and [attention state](../attention-state/): capture coverage and model-specific persistent state.
