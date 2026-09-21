# GLM-5.3 on the vLLM 0.30 MetaX source pair

This directory contains the source-patch bundle for GLM-5.3-Flash BF16 on the vLLM 0.30 development core and MetaX 0.29 development plugin.

The bundle is intentionally separate from the generic MetaX 0.26 installer. It is not selected by the 0.26 release path and does not claim that the public MetaX 0.26 release supports GLM-5.3.

## Patch order

Apply these patches in order to the pinned source pair:

1. `metax-glm53-typed-kv-layout.patch`
   Aligns MetaX FlashAttention and sparse MLA with vLLM's typed `KVCacheLayout` contract and preserves HMA physical row strides.
2. `metax-glm53-mla-prefill-fallback.patch`
   Removes the MetaX platform override that forced an unsupported dense MLA prefill backend, allowing GLM's existing sparse MQA fallback.
3. `metax-vllm030-fused-moe-all2all-api.patch`
   Migrates the MetaX all-to-all helper from the removed boolean argument to vLLM 0.30's all-to-all manager contract.
4. `metax030-glm-image-token-mtp.patch`
   Routes `Glm5NextForConditionalGeneration` through the existing MTP `image_token_id` compatibility branch.
5. `metax-sparse-mla-decode-metadata.patch`
   Carries the maximum decode query length supplied by vLLM's sparse MLA metadata builder.
6. `metax-sparse-mla-nope-query.patch`
   Copies the NoPE query into the existing buffer when the RoPE component is empty, retaining the fused concatenation kernel for nonempty RoPE.
7. `metax-paged-mqa-schedule.patch`
   Builds the paged-MQA schedule with MetaX DeepGEMM instead of consuming the uninitialized buffer left by the upstream CUDA-only metadata path.

The patches target the exact source pair recorded by the corresponding source-built runtime. Rebase or upgrade the source pair only after regenerating and validating the bundle; do not silently apply it to an unrelated release.

## Validation boundary

The first patch bundle has passed C500 layout, HMA stride, BF16 prefill and six-token decode checks. The remaining patches have passed source-level and packaged import checks. Full two-node GLM weight loading, MTP initialization, KV transfer, and OpenAI-compatible generation remain the runtime acceptance boundary.

MTP requires its full sparse indexer and separate MLA, compressed-indexer, and tail cache groups. Use the pinned core's Model Runner V2 (`VLLM_USE_V2_MODEL_RUNNER=1`) for multi-group draft attention; the legacy proposer assumes a single draft KV-cache group. Each proposal computes MTP-specific top-k indices in its first step and reuses those indices only in subsequent draft steps.
