# GLM-5.3 on the vLLM 0.30 MetaX source pair

This directory is the versioned source-patch bundle for the GLM-5.3-Flash BF16 runtime validated with the vLLM 0.30 development core and the MetaX 0.29 development plugin.

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

The patches target the exact source pair recorded by the corresponding source-built runtime. Rebase or upgrade the source pair only after regenerating and validating the bundle; do not silently apply it to an unrelated release.

## Validation boundary

The first patch bundle has passed C500 layout, HMA stride, BF16 prefill and six-token decode checks. The remaining patches have passed source-level and packaged import checks. Full two-node GLM weight loading, MTP initialization, KV transfer, and OpenAI-compatible generation remain the runtime acceptance boundary.

5. `metax030-glm-mtp-kv-group.patch`
   Keeps MTP layers on the shared target sparse-MQA top-k path instead of creating MTP-local indexer and tail KV groups.
