<!--
SPDX-License-Identifier: Apache-2.0
SPDX-FileCopyrightText: Copyright contributors to the Foretoken project
-->

# Reduce launch overhead with workload-aware graph bucketing

English | [简体中文](launch-graph-buckets_zh.md)

## Observations and Motivation

Short decode kernels can leave the GPU waiting between host launches. CUDA Graph replay reduces those submissions, but serving batches vary in token count, request geometry, speculative length, and active adapters. Capturing every combination consumes startup time and memory; coarse buckets execute unnecessary padded work.

## Principles and Analysis

Replay uses the operations and addresses recorded during capture. Several compatible batches can share a captured size by padding. For an actual token count b and captured count B, the extra B−b positions can still consume computation even though their outputs are discarded.

The relevant cost is `execution(padded shape) + dispatch/launch cost`. Graph coverage alone is insufficient: broader coverage can increase padding, and graph memory competes with KV capacity. The distribution of engine steps, rather than client concurrency alone, determines this trade-off.

## Design

vLLM v0.31.0 registers FULL and PIECEWISE keys. FULL captures complete eligible execution; PIECEWISE captures compatible regions while other operations remain outside. Keys include token count, request geometry, and LoRA state where needed. Uniform speculative decode can have multiple query tokens per request.

PIECEWISE keys relax request count and uniformity; FULL retains geometry used by backend metadata. Capture sets reflect the attention backend, hybrid layers, and compiler mode. Unsupported operations require a different execution path rather than simply more token buckets.

## Implementation

The dispatcher precomputes token-count-to-capture-size mapping and registers valid keys after attention capability is known. Runtime selection is:

```text
if graphs are disabled or the batch exceeds the capture limit:
    return eager execution
pad token count to its capture bucket
build descriptor with request geometry and adapter state
if an allowed FULL key matches:
    replay FULL
else if an allowed relaxed PIECEWISE key matches:
    replay PIECEWISE regions
else:
    execute eagerly
```

Graph wrappers consume the selected key and mode. Capture-size choices and backend compatibility remain with the engine; changing request-count fields arbitrarily can invalidate captured attention metadata.

## Impact and Results

Replay replaces repeated host kernel submissions with a graph launch, reducing exposed launch gaps in small-compute steps. Workload-aligned buckets reduce padded execution while retaining replay for frequent shapes.

More buckets add capture work and retained memory. If that memory displaces KV, lower launch cost can coincide with fewer resident sequences or more preemption. Full replay also remains limited by participating operations, so a batch that requires unsupported behavior stays on the piecewise or eager path.

## References

- [vLLM v0.31.0 CUDA Graph design](https://docs.vllm.ai/en/v0.31.0/design/cuda_graphs/).
- [Dispatcher implementation](https://github.com/vllm-project/vllm/blob/db9527a46873454610df6dbedf79a36d6bf1a7f6/vllm/v1/cudagraph_dispatcher.py): padding, registered keys, LoRA cases, and selection order.
- [PyTorch v2.14 CUDA Graph Trees](https://github.com/pytorch/pytorch/blob/v2.14.0/docs/source/user_guide/torch_compiler/torch.compiler_cudagraph_trees.md): concrete-shape capture and memory lifetimes.
