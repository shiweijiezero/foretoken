<!--
SPDX-License-Identifier: Apache-2.0
SPDX-FileCopyrightText: Copyright contributors to the Foretoken project
-->

# Experimental Draft/Target deployment

[中文](README_zh.md)

Deploy independent Draft and Target Pools, discovered and selected by the normal
frontend Router. This example needs two GPUs, a shared RuntimeCache, and a
source-built platform with the [external-speculation engine extension](../../data-plane/dt-plugin/docs/mrv2-integration.md).
The released and repository-pinned vLLM engines do not provide that extension.
Set the platform's `runtime.vllm.image` to your model-server image containing it;
installing the Python DT package alone does not add the engine APIs.

From the repository root, after installing that platform:

```bash
foretoken deploy examples/draft-target --timeout 20m
FRONTEND_URL="$(foretoken endpoint examples/draft-target)"
curl --fail-with-body "$FRONTEND_URL/v1/chat/completions" \
  -H 'Content-Type: application/json' \
  -d '{"model":"Qwen/Qwen3-4B","messages":[{"role":"user","content":"Hello"}],"temperature":0,"max_tokens":256}'
```

`spec.model` selects Target. `modelPools[].model` is required for Draft and is
invalid on other roles. Both inherit the service's model source and tokenizer;
by default the tokenizer comes from Target. Choose models with compatible token
IDs, not just similar names. For `source: local`, this initial deployment requires
absolute model and tokenizer paths visible inside the Pods.

Change each Pool's `replicas` independently and deploy again. New requests choose
one available Draft and one Target. Scaling down closes admission, withdraws the
route, and waits for existing sessions within the configured drain deadline.
Multiple Draft replicas provide capacity; they do not jointly propose a tree.

Current limits: text input, greedy sampling, one GPU per role instance, no P/D
composition, multimodal input, KV offload/transfer, structured output, or profiling.
Scheduler telemetry is not yet exposed by the DT role, so performance-based
autoscaling policies have not been validated. Candidate tokens travel over HTTP;
the separate Mooncake diagnostic is not part of this inference path.

```bash
foretoken delete examples/draft-target
```
