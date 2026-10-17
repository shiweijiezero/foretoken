<!--
SPDX-License-Identifier: Apache-2.0
SPDX-FileCopyrightText: Copyright contributors to the Foretoken project
-->

# Reduce visual prefill through encoder and KV reuse

English | [简体中文](visual-prefill-reuse_zh.md)

## Observations and Motivation

Repeated questions about one image repeat both visual encoding and decoder prefill. These are different reuse opportunities: the encoder embedding depends on image processing and encoder weights, whereas decoder KV also depends on preceding text and positions. VLCache saves contextual visual-prefill work through selective recomputation; it is not simply the exact encoder cache already available in engines.

## Principles and Analysis

Write the two quantities as `E(image)` and `KV_l(image, preceding_text, positions)`. An unchanged processed image can reuse `E` exactly. Reusing KV after the question or preceding instructions change is generally approximate, even with the same picture.

Visual size amplifies the cost. Qwen2.5-VL uses 14-pixel spatial patches and merges four neighboring features, giving approximately `HW/784` language-side visual tokens. A 448×448 image yields 256; doubling both dimensions yields 1024. Processor framing and temporal grouping add model-specific details. Large visual prefixes make decoder-side reuse more valuable than an encoder hit alone.

## Design

On the first image occurrence, store its embedding and per-layer visual KV before rotary position encoding. A later exact-image hit retrieves them, adjusts positional handling, and recomputes a selected prefix of visual tokens while reusing the rest. A small calibration set determines layer-dependent recomputation fractions, constrained not to increase with depth.

This is exact-image lookup with approximate cross-context state reuse, not similarity matching between different images. VLCache describes Tair KVCache Store and a SGLang integration but identifies no public author code repository. The existing vLLM encoder cache implements the exact embedding path, not this selective contextual-KV algorithm.

## Implementation

The selective path is:

```text
image = checkpoint_processor(input_image)
entry = lookup_exact_image(image, encoder_and_model_revision)
if entry is absent: encode image and compute/store pre-RoPE visual KV
bind cached embedding into the current text-and-image sequence
for decoder layer l:
    m = calibrated_prefix_count(l, visual_token_count)
    compute current-context KV for the first m visual tokens
    reuse stored pre-RoPE KV for remaining visual tokens
    apply current positional encoding; attend with fresh current queries
return generated answer; retain the image entry for later requests
```

Use the released checkpoint and its processor together; calibration chooses reuse fractions rather than training new weights. The paper evaluates Qwen2.5-VL and Qwen3-VL. Encoder/tower adapters, image ordering, or processor changes alter the validity of the entry. BF16/FP16 does not make contextual KV reuse exact.

## Impact and Results

VLCache v1 measures repeated-image TTFT with 20 text tokens and up to 20K image tokens. On H20-3e, Qwen3-VL-8B and 32B reach reported maximum speedups of 1.88× and 3.94× at 20K image tokens. Dynamic recomputation averages 3.5% for the 8B model; mean benchmark accuracy is 74.49 versus 74.48 with full recomputation.

Quality is not uniformly identical: Qwen2.5-VL-7B's dynamic 10% recomputation scores 64.72 versus 65.37 with full recomputation. Encoder hits remove ViT/projector work; selective KV reuse additionally removes visual decoder prefill, with cache storage and contextual approximation as costs. Small-font OCR and region-specific questions can be more sensitive than the aggregate score suggests.

## References

- [VLCache, arXiv:2512.12977v1](https://arxiv.org/html/2512.12977v1): December 2025 method and experiments; no author code link identified in the paper.
- [Qwen2.5-VL report, arXiv:2502.13923v1](https://arxiv.org/html/2502.13923v1): processor, spatial merging, training, and release links.
- [vLLM encoder-cache owner](https://github.com/vllm-project/vllm/blob/6849ca696ac9fbecec29a8a8c47f6ee18aea1ee9/vllm/v1/core/encoder_cache_manager.py): source at commit `6849ca6`; exact embedding reuse is implemented.
