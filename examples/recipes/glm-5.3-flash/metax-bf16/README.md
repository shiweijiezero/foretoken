<!-- SPDX-License-Identifier: Apache-2.0 -->
<!-- SPDX-FileCopyrightText: Copyright contributors to the Foretoken project -->

# GLM-5.3-Flash BF16 on MetaX C500

English | [简体中文](README_zh.md)

Run one Foretoken model group on two nodes with eight C500 64 GiB GPUs each: attention TP8×DP2, EP16, a 1,048,576-token context and native MTP with five speculative tokens. The frontend uses KV-aware routing. Two memory clients contribute 1 TiB each to one shared Mooncake Store, without SSD offload.

Use a source-built MetaX vLLM runtime supporting GLM-5.3-Flash, MTP and hybrid attention caches. The current release's default inference image does not support this combination.

## Prepare

Use the [MetaX platform guide](../../../../docs/development/metax-platform.md) to install the platform with a compatible runtime image. Nodes must advertise GPU and shared RDMA resources and allow the runtime to lock memory for RDMA.

Set the environment-specific inputs before deploying:

- Set `directory` in `cache.yaml` to a shared data root visible from both nodes. Place the complete `zai-org/GLM-5.3-Flash-BF16` checkpoint under `models/zai-org/GLM-5.3-Flash-BF16/` within that root. BF16 weights occupy approximately 599 GiB. See [model storage](../../../../docs/model-storage.md) for permissions and other sources.
- Build and distribute the Store image as described in the [shared KV example](../../../shared-kv-store/README.md). Set both `image` fields in `kvservice.yaml` to its actual reference.
- The example uses the platform-managed `rdma/foretoken_rdma` resource. When the platform reuses an external RDMA allocation, set `rdmaResourceName` in `kvservice.yaml` to that resource instead.

The configuration requests 16 GPUs, 76 CPU cores and 3208 GiB of host memory in total. Each model member requests eight GPUs and 512 GiB of memory. Adjust resource requests and limits to the available nodes.

## Deploy and request

Run from the repository root:

```bash
RECIPE=examples/recipes/glm-5.3-flash/metax-bf16
foretoken deploy "$RECIPE" --timeout 70m
FRONTEND_URL="$(foretoken endpoint "$RECIPE")"

curl --fail-with-body --no-buffer \
  "$FRONTEND_URL/v1/chat/completions" \
  -H 'Content-Type: application/json' \
  -d '{"model":"zai-org/GLM-5.3-Flash-BF16","messages":[{"role":"user","content":"Hello, please introduce yourself."}],"max_tokens":512,"stream":true}'
```

This example uses the platform's direct LoadBalancer endpoint. For Gateway mode, configure the hostname and request Host as described in the [MetaX deployment guide](../../../../docs/metax-deployment.md).

Reapply the same `foretoken deploy` command after changing configuration. Inspect or remove the deployment with:

```bash
foretoken status "$RECIPE"
foretoken delete "$RECIPE"
```

Deletion removes the namespace's model, frontend and memory Store. Checkpoint files in the shared directory remain; in-memory KV entries do not.
