<!--
SPDX-License-Identifier: Apache-2.0
SPDX-FileCopyrightText: Copyright contributors to the Foretoken project
-->

# Balance custom kernel control and compiler-visible fusion

English | [简体中文](custom-op-fusion_zh.md)

## Observations and Motivation

A faster native kernel can make a compiled layer slower if its boundary prevents surrounding fusion, inserts a copy, or breaks graph capture. Integrating the kernel therefore requires a compiler interface as well as correct arithmetic.

## Principles and Analysis

A registered custom operator exposes a schema, dispatch implementations, and a metadata-only FakeTensor function. Shapes, strides, dtype, device, mutation, and aliasing describe what downstream transformations may assume. The compiler can trace the call while treating the native implementation as opaque.

For `normalization → conversion → projection`, an opaque replacement may hide a conversion that previously fused with projection. Incorrect output strides can select an incompatible consumer; undeclared mutation can invalidate reuse or reordering. Boundary placement changes both optimization visibility and correctness.

## Design

Three designs offer different control: visible tensor expressions let the compiler optimize the sequence; an opaque operator owns a hand-tuned kernel; a pattern pass replaces a recognized sequence with that operator only for supported shapes and layouts.

vLLM uses the third design for Q/K normalization and RoPE. Cache lifetime stays with the engine, while the operator explicitly declares its writes and aliases.

## Implementation

The integration follows a short sequence:

```text
register schema, including mutated arguments and aliases
register backend kernel and metadata-only FakeTensor function
trace the model with symbolic metadata
match a supported operator pattern
replace it with the native call, preserving output metadata
partition graph-unsafe work outside captured regions
```

vLLM's QK pass matches packed-QKV splitting, per-head RMSNorm, and RoPE. It calls the native fused operation through `auto_functionalized` and splits the returned updated tensor, preserving a functional graph result around a mutating native implementation.

PyTorch treats custom operators as capture-safe by default. An operation containing host/device synchronization or other incompatible behavior uses `torch._C.Tag.cudagraph_unsafe`; CUDA Graph partitioning can then leave that operation outside captured regions. `opcheck` checks registration integration, while output comparisons establish the algorithm's numerical behavior.

## Impact and Results

Correct metadata and mutation declarations let compiled consumers use the native kernel without unnecessary graph breaks. Pattern replacement can preserve neighboring optimization until the supported fused sequence is selected.

Opaque boundaries reduce compiler visibility, so fewer source-level operations do not necessarily mean less device work. Added materialization, staging, or graph partitions can erase the kernel's savings. The useful effect is removing those costs across the complete producer–consumer sequence.

## References

- [PyTorch custom C++/CUDA operators](https://docs.pytorch.org/tutorials/advanced/cpp_custom_ops.html): schema, FakeTensor, mutation, and integration checks.
- [vLLM v0.31.0 compilation design](https://docs.vllm.ai/en/v0.31.0/design/torch_compile/): custom attention boundaries and piecewise compilation.
- [QK fusion implementation](https://github.com/vllm-project/vllm/blob/db9527a46873454610df6dbedf79a36d6bf1a7f6/vllm/compilation/passes/fusion/qk_norm_rope_fusion.py).
- [PyTorch v2.14 graph partitioning](https://github.com/pytorch/pytorch/blob/v2.14.0/docs/source/user_guide/torch_compiler/torch.compiler_cudagraph_trees.md): unsafe custom operators and capture behavior.
