<!-- SPDX-License-Identifier: Apache-2.0 -->
<!-- SPDX-FileCopyrightText: Copyright contributors to the Foretoken project -->

# Draft/Target disaggregation

English | [简体中文](README_zh.md)

Serve `Qwen/Qwen3-8B` with an EAGLE3 Draft on a separate GPU. The Draft proposes tokens; the Target verifies them and returns the response through the ordinary generation API. Several Target replicas can share the Draft.

The example uses a [source-installed platform](../../../docs/deployment/source.md) with its vLLM 0.31 environment and two GPUs, one per model instance. It requests 10 CPU cores and 98 GiB of host memory including the frontend. Both checkpoints are public, and model files use the platform's default storage. This configuration serves text requests without LoRA.

## Deploy and send a request

Run from the repository root:

```bash
foretoken deploy examples/feature/draft-target --timeout 20m
FRONTEND_URL="$(foretoken endpoint examples/feature/draft-target)"
REQUEST_HOST="$(foretoken endpoint examples/feature/draft-target --host)"

curl --fail-with-body --no-buffer "$FRONTEND_URL/v1/chat/completions" \
  -H "Host: $REQUEST_HOST" \
  -H 'Content-Type: application/json' \
  -d '{"model":"Qwen/Qwen3-8B","messages":[{"role":"user","content":"Explain how speculative decoding works."}],"max_tokens":256,"stream":true}'
```

Only the Target model is exposed to callers. The Draft Pool's `model` selects its checkpoint, and the Target's `speculation.draftPools` refers to that Pool's `name` in the same model service. Algorithm options stay together in the Draft's native `speculative-config`.

## Share the Draft

In `model.yaml`, change the `target` Pool's `replicas` to `2` or `3`, then rerun the deployment command. These configurations use three or four GPUs in total. Each Draft or Target instance uses one GPU; replica counts do not change engine tensor parallelism.

For shared-Draft performance, use `distributed-executor-backend: uni` in the Draft Pool's `engineArgs`. Other supported single-worker executors use ordinary proposal scheduling. The Draft's native batch limits apply to the combined workload from its Targets; requests beyond those limits run in subsequent batches.

`async-scheduling` can be enabled or disabled independently in each Pool's `engineArgs`. Set it to `false` in both Pools for a synchronous comparison. EAGLE3 requires features from the complete prompt, so prefix caching is disabled automatically for this configuration.

For performance measurements, use the original [ShareGPT conversations](https://huggingface.co/datasets/anon8231489123/ShareGPT_Vicuna_unfiltered) through the [benchmark dataset input](../../../benchmarks/docs/perf/huggingface.md), and count both Draft and Target GPUs. Compare with colocated decoding using the same models, candidate count, scheduling mode, prefix-cache setting, and workload.

## Clean up

```bash
foretoken delete examples/feature/draft-target
```
