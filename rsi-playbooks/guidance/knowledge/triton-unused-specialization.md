<!-- SPDX-License-Identifier: Apache-2.0 -->
<!-- SPDX-FileCopyrightText: Copyright contributors to the Foretoken project -->

# Avoid Unused Triton Specializations

English | [简体中文](triton-unused-specialization_zh.md)

A compile-time argument can disappear from optimized GPU code while still selecting a separate Triton kernel variant. When a branch makes a layout parameter irrelevant, pass a canonical value at the launch boundary instead of incidental tensor metadata.

## Example: a packed batch stride

Variable-length sequences are often packed into a tensor with a singleton outer batch dimension. Its batch stride changes with the total token count, although the kernel always addresses batch zero:

```python
input_batch = 0 if IS_VARLEN else sequence_index
address = base + input_batch * stride_batch + token_offset
```

If `stride_batch` is a `tl.constexpr`, different values can trigger compilation even when `IS_VARLEN=True` makes the multiplication disappear. The launcher can express the actual addressing requirement:

```python
stride_batch = 0 if cu_seqlens is not None else tensor.stride(0)
```

Keep real strides for fixed-length batches. Token, head and dimension strides still contribute to addresses and cannot be normalized merely because tensors happen to have similar shapes.

This applies to the Q/K/V batch strides in `fused_sigmoid_gating_delta_rule_update`, whose kernel uses `input_batch = 0 if IS_VARLEN else i_n`. Check the complete launcher and kernel in the engine revision being used; the wrapper must also enforce the packed-input contract.

## Establish the cause

Start with a real first-request delay or repeated compilation, then compare the affected kernel's signatures, compile options and optimized IR. Identical source text alone does not establish equivalent specializations: launch parameters and compiler options can differ.

For the GDN case measured on MetaX C500 with Triton 3.6, packed total lengths 4, 8 and 12 produced three original VARLEN variants. Their optimized TTIR and TTGIR were byte-identical. The launcher supplied different Q/K/V batch strides although the VARLEN branch multiplies them by zero. Normalizing those three launcher arguments reduced VARLEN variants from three to one. A true fixed-length batch of size two retained one variant in each arm, reducing the total from four to two.

Each arm ran in a fresh process with a separate empty Triton cache. Outputs and complete mutable state-page storage were equal under `torch.equal` for all four inputs. This established numerical equality for those cases, not bit-pattern equality or coverage of every state transition.

## Validate the owning boundary

Change only arguments proven irrelevant to the selected branch. Exercise both that branch and the path that still needs real strides, comparing outputs and the complete mutable state. Classify cache records by logical kernel and signature rather than counting every file in the cache.

Then measure the service separately. Fewer compiled variants can reduce preparation or first-request work without changing warm kernel execution. Report first-request latency and steady-state throughput independently, retaining the original first-request cost in the benchmark result. The four-to-two diagnostic above established specialization reduction; its process runtime was not a measurement of compilation savings or serving acceleration.

Implementation entry points: `fused_sigmoid_gating_delta_rule_update` and `fused_sigmoid_gating_delta_rule_update_kernel` in vLLM's `third_party/flash_linear_attention/ops/fused_sigmoid_gating.py`. The [Triton JIT API](https://triton-lang.org/main/python-api/generated/triton.jit.html) also exposes `do_not_specialize` for runtime arguments; it does not make an unnecessary `tl.constexpr` value cease to distinguish compiled variants.
