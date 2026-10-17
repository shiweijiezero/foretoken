<!--
SPDX-License-Identifier: Apache-2.0
SPDX-FileCopyrightText: Copyright contributors to the Foretoken project
-->

# Balance compilation cost with targeted shape specialization

English | [简体中文](compile-cost-specialization_zh.md)

## Observations and Motivation

Serving token counts change continually. Static compilation can generate repeated variants, while fully dynamic code may miss kernels optimized for common sizes. Short-lived workers can spend a substantial fraction of their lifetime preparing code rather than serving requests.

## Principles and Analysis

Symbolic dimensions let one compiled artifact accept several sizes, but control-flow conditions still create guards. A single-token branch and a multi-token branch using different algorithms need different paths. Triton `constexpr` arguments or custom operators converting symbolic dimensions to integers can also force specialization.

Compilation and CUDA Graph capture are separate layers: one symbolic Inductor graph can accept several sizes, while replay usually needs separate concrete-shape recordings. Fewer recompilations do not necessarily mean fewer captures or less padding.

If specialization adds preparation cost `C` and saves `Δt > 0` per use, `ceil(C/Δt)` uses cover that cost when other costs are omitted. Shape frequency and worker lifetime determine whether the investment pays.

## Design

Keep stable head dimensions and matrix widths static, and represent frequently changing token counts symbolically. Add static variants only for frequent shapes with useful execution savings. Choose capture sizes separately according to padding and memory cost.

vLLM v0.31.0's compilation integration uses a symbolic token dimension and adds static variants through `compile_sizes`. That entry point owns model compilation.

## Implementation

PyTorch v2.14 initially compiles with static sizes; after a size-triggered recompile, changed dimensions can become dynamic. Known varying dimensions can be annotated with `mark_dynamic` outside the compiled function. `maybe_mark_dynamic` permits legitimate branch specialization.

Execution selection can be summarized as:

```text
prepare the general symbolic path and selected frequent static paths
for each execution:
    use a matching static path if its guards hold
    otherwise use a symbolic path whose guards hold
    let graph dispatch separately select padding and captured replay
```

A guard failure points to a branch or metadata conversion. Preserve branches representing different algorithms; eliminate accidental concretization when the computation is equivalent.

## Impact and Results

The PyTorch dynamic-shape documentation's simple example calls lengths 10, 20, 30, and 40. Static mode generates four graphs; dynamic mode generates one. The difference is recompilation caused by those size changes.

Targeted specialization retains common-shape optimization while limiting artifact count and startup work. Rare shapes or short-lived replicas may never amortize preparation. Excessive capture sizes also consume memory otherwise available to KV caches.

## References

- [PyTorch v2.14 dynamic shapes](https://github.com/pytorch/pytorch/blob/v2.14.0/docs/source/user_guide/torch_compiler/torch.compiler_dynamic_shapes.md): annotations, guards, and graph-count example.
- [vLLM v0.31.0 compilation integration](https://docs.vllm.ai/en/v0.31.0/design/torch_compile/): symbolic tokens and static compilation sizes.
- [PyTorch v2.14 CUDA Graph Trees](https://github.com/pytorch/pytorch/blob/v2.14.0/docs/source/user_guide/torch_compiler/torch.compiler_cudagraph_trees.md): shape-specific capture; see also [graph buckets](launch-graph-buckets.md).
