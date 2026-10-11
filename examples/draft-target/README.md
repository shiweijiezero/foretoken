<!--
SPDX-License-Identifier: Apache-2.0
SPDX-FileCopyrightText: Copyright contributors to the Foretoken project
-->

# Draft/Target deployment

[中文](README_zh.md)

Serve `Qwen/Qwen3-4B` with a separate `Qwen/Qwen3-0.6B` Draft, then call the
public model through the OpenAI-compatible API. Both models use Aggregate Pools;
you can scale their replicas independently.

This Kubernetes example uses an independent autoregressive Draft. To run DFlash,
EAGLE/EAGLE3, DSpark or MTP, use the [standalone neural Draft guide](../../data-plane/dt-plugin/README.md#run-a-neural-draft-pair-on-nvidia).
Neural `speculative-config` is not currently exposed by this ModelService path.

## Deploy and send a request

Use a GPU-enabled Kubernetes cluster with at least two available GPUs and shared
model storage. The default resources request 4 CPU and 16 GiB RAM for Draft,
4 CPU and 32 GiB RAM for Target, plus 2 CPU and 2 GiB RAM for the frontend.
The example's [RuntimeCache](cache.yaml) uses the repository's `data/` directory;
follow [model storage](../../docs/model-storage.md) for the cluster's shared storage setup.

DT requires the source-built platform and its supported engine. NVIDIA builds
prepare the CUDA 13 runtime and DT plugin automatically; hosts need a compatible
driver and GPUs with compute capability 8.0 or newer for the default batch-invariant
mode. For MetaX or custom engine images, use the [role compatibility requirements](../../data-plane/dt-plugin/README.md#start-two-roles).
See [source installation](../../docs/custom-deployment.md) for build prerequisites.

From the repository root, install the source CLI and platform, then deploy:

```bash
python -m pip install -e .
foretoken install -e .
foretoken deploy examples/draft-target --timeout 20m
export FRONTEND_URL="$(foretoken endpoint examples/draft-target)"

curl --fail-with-body "$FRONTEND_URL/v1/chat/completions" \
  -H 'Content-Type: application/json' \
  -d '{"model":"Qwen/Qwen3-4B","messages":[{"role":"user","content":"Explain speculative decoding in two sentences."}],"temperature":0,"max_tokens":128,"chat_template_kwargs":{"enable_thinking":false}}'
```

If this checkout's source platform is already installed, start with `foretoken deploy`.
The response contains the Target-confirmed answer in `choices[0].message.content`
and token counts in `usage`. `enable_thinking: false` makes this Qwen3 example
answer directly. Add `"stream": true` to the JSON and `--no-buffer` to curl to receive SSE output.
The minimal deployment needs no RDMA and uses `temperature: 0`.

## Call from Python

Install the SDK with `python -m pip install openai`, then use the same exported
`FRONTEND_URL`:

```python
import os
from openai import OpenAI

with OpenAI(
    base_url=os.environ["FRONTEND_URL"].rstrip("/") + "/v1",
    api_key="unused",
) as client:
    response = client.chat.completions.create(
        model="Qwen/Qwen3-4B",
        messages=[{"role": "user", "content": "Explain speculative decoding in two sentences."}],
        temperature=0,
        max_tokens=128,
        extra_body={"chat_template_kwargs": {"enable_thinking": False}},
    )
    print(response.choices[0].message.content)
    print(response.usage)
```

`api_key` satisfies the SDK interface; this example connects directly to the
frontend without an authentication gateway. Applications use the Target model ID
and ordinary messages; they do not manage Draft sessions or verification rounds.

## Choose models and replica counts

Edit [model.yaml](model.yaml):

| Setting | Meaning |
| --- | --- |
| `spec.model` | Public Target model; update the client model ID when changing it. |
| `spec.speculation.draftPool` | Name of the Draft Pool, `draft` in this example. |
| Draft Pool `model` | Draft weights; only the referenced Draft Pool may override the service model. |
| Each Pool's `replicas` | Independent instance count; each instance uses one GPU. |
| `spec.engineArgs.max-model-len` | Context limit applied to both roles; prompt plus output must fit. |

Both Pools keep `role: aggregate`. They inherit the service's model source and
tokenizer, normally the Target tokenizer. Choose models with compatible token-ID
meanings. For `source: local`, model and tokenizer paths must be absolute and
visible inside the Pods.

For two Drafts and two Targets, set `replicas: 2` in both Pool entries and run
`foretoken deploy examples/draft-target --timeout 20m` again. This needs four GPUs
and twice the model-pool CPU/RAM; the frontend configuration and client API stay
the same. To change only Draft capacity, change only its Pool's replica count.
Multiple Draft replicas provide capacity; they do not jointly propose a tree.

For a Target-only comparison, remove `spec.speculation` and the Draft Pool from
`model.yaml`, then redeploy. Keep the Target model and client sampling settings
the same.

The frontend schedules ready requests each round and can select a different Draft
for the next round. Existing Target sessions stay on their owner. Scale-down drains
existing sessions; a rollout without spare GPUs can temporarily interrupt service
availability. [Frontend lifecycle details](../../data-plane/dt-plugin/docs/frontend-workflow.md)
cover cancellation, serving updates and custom scheduling.

## Enable random sampling

On RDMA-capable clusters, `foretoken install` discovers an existing shared RDMA
allocation or prepares the shared device plugin. Override `rdma.resourceName`
and `rdma.resourceCount` in platform values only when selecting an external
allocation. With an available allocation, the controller gives both roles RDMA
devices and enables Mooncake transfer. Workers advertise Pod
IPs; networking must permit role HTTP and dynamic Mooncake handshake ports. The
runtime needs a compatible Mooncake build and GPU registration support; resource
allocation alone does not establish the physical transfer path.

With this transport enabled, requests may use `temperature`, `top_p`, `top_k` and
a Target `seed`. Draft randomness is independent of that seed. Without RDMA,
random-sampling requests are rejected.

## Limits and further experiments

The serving path supports text and linear candidates, with one eager GPU worker
per role and synchronous local scheduling. It does not support P/D composition,
multimodal input, KV offload, structured output, non-default penalties, `min_p`
or profiling. A model's non-default generation penalties also cause rejection;
see [sampling requirements](../../data-plane/dt-plugin/README.md#start-two-roles).

Use the [batch example](../../data-plane/dt-plugin/docs/frontend-workflow.md#run-per-round-batches-through-router)
for custom scheduling and the [migration API](../../data-plane/dt-plugin/docs/frontend-workflow.md#move-a-request-between-targets)
for caller-selected Target KV migration. Public requests do not migrate automatically.
Kubernetes RDMA networking, metric-driven autoscaling and performance benefits
remain unverified.

## Delete the deployment

```bash
foretoken delete examples/draft-target
```
