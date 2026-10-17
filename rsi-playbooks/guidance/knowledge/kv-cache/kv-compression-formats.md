<!--
SPDX-License-Identifier: Apache-2.0
SPDX-FileCopyrightText: Copyright contributors to the Foretoken project
-->

# Choose KV Quantization or Storage Codecs for Compression

English | [简体中文](kv-compression-formats_zh.md)

## Observations and Motivation

“Compressed KV” can mean lower-precision resident tensors or an encoded storage object that expands before attention. The former can reduce runtime memory and reads; the latter primarily reduces storage and transfer bytes. Confusing them produces incorrect capacity and performance predictions.

## Principles and Analysis

[KIVI, February 2024](https://arxiv.org/html/2402.02750v1) uses asymmetric quantization: keys per channel across tokens, values per token across channels. Persistent key-channel outliers and token-dependent value ranges motivate different axes. For each group, reconstruct `x_hat = s q + z`, where `B` is the bit width, `z = min(x)` and `s = (max(x) - min(x))/(2^B - 1)`. The quantized integers, scales and offsets all consume memory.

Keys need enough arriving tokens to form a group. KIVI retains a recent full-precision residual window, with example group size 32 and residual length 128. A 2-bit payload is nominally one eighth of BF16; metadata, residuals, padding and workspace reduce actual savings, especially for short histories.

Storage-codec loading costs `T_fetch(D_compressed) + T_decode + T_install` before accounting for actual overlap; resident tensors can remain near uncompressed size. Runtime quantization changes `T_read + T_dequant + T_compute`, including packing and scale access. Smaller representations can be slower when kernels are inefficient or attention is not the bottleneck.

## Design

Choose the representation at its consumer. KIVI keeps a quantized history plus a recent full-precision residual window and gives attention access to both; the key and value quantizers use different axes. CacheGen stores multiple encoded context variants and reconstructs tensors before installation, allowing the loader to trade network bytes against decode and recomputation cost.

Runtime FP8 in vLLM is a separate representation: calibrated scales, per-tensor or backend-supported per-head scaling, and layer exclusions determine what attention reads. Unit scales and calibrated scales produce different numerical conditions. A storage codec does not by itself change the resident attention format.

## Implementation

KIVI's append-and-attend algorithm is:

1. Append new K/V to the residual window. When old keys form a token group, compute per-channel ranges; compute value ranges per token across channel groups.
2. For nonconstant groups, quantize with `q = round((x-z)/s)` clipped to the integer range. A constant group uses zero codes and reconstructs as `z`, with scale storage following the quantizer's convention. Pack integers and retain scales/offsets.
3. During attention, dequantize old states as needed and combine with recent full-precision states.

CacheGen's storage algorithm is:

1. Split into ten-token chunks; encode the first token's K/V as an 8-bit anchor.
2. Quantize differences from the reconstructed anchor at layer-dependent precision, then arithmetic-code the symbols.
3. Choose a stored encoding or text recomputation by loading cost. For an encoding, decode symbols, reconstruct anchors plus deltas, then install K/V.

LMCache exposes the CacheGen serializer through `remote_serde: "cachegen"`; its deprecated in-process setup is not the MP configuration. vLLM's NIXL endpoints additionally require compatible scale representations; static or packed-scale support does not imply dynamic-scale interoperability.

## Impact and Results

KIVI evaluates Llama-2-7B on one 80 GB A100 with a ShareGPT-derived workload averaging 161 input and 338 generated tokens. Against FP16, KIVI-2 with residual lengths 32 or 128 reports 2.6× lower peak memory including weights, up to 4× larger batches at comparable maximum memory, and 2.35–3.47× throughput. Batches are increased to the memory limit; this is a capacity-enabled throughput comparison rather than an equal-batch kernel speedup.

CacheGen reduces encoded storage and transfer bytes but adds decoding; resident tensors expand before attention. Runtime quantization instead reduces resident bytes and reads while adding packing, scale access and dequantization. Neither the nominal bit-width ratio nor entropy coding alone determines service speed.

## References

- [KIVI, February 2024](https://arxiv.org/html/2402.02750v1)
- [CacheGen, October 2023](https://arxiv.org/html/2310.07240v1)
- [quantized-KV documentation](https://docs.vllm.ai/en/latest/features/quantization/quantized_kvcache/)
- [CacheGen guide](https://github.com/LMCache/LMCache/blob/4d69cdc43ac9bab9c0c1cd5cac380c480cb32224/docs/source/kv_cache_optimizations/compression/cachegen.rst)
- [NIXL compatibility rules](https://github.com/vllm-project/vllm/blob/6849ca696ac9fbecec29a8a8c47f6ee18aea1ee9/docs/features/nixl_connector_compatibility.md)
