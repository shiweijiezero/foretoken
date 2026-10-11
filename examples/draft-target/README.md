<!--
SPDX-License-Identifier: Apache-2.0
SPDX-FileCopyrightText: Copyright contributors to the Foretoken project
-->

# Draft/Target deployment

[中文](README_zh.md)

Deploy the main model and a separate Draft model in independent Aggregate Pools,
discovered and selected by the normal frontend Router. This example needs two
GPUs, a shared RuntimeCache, and a
source-built platform with the [DT plugin and its supported native vLLM version](../../data-plane/dt-plugin/docs/mrv2-integration.md).
NVIDIA source builds prepare the supported CUDA 13 engine and install the DT package
automatically. Use a CUDA 13-compatible host driver. Existing release images are
not upgraded by deploying this example; rebuild the platform from source first.
Custom engine images must match the plugin's supported native version.

With the platform's existing `rdma.resourceName` and `rdma.resourceCount` configured,
the controller allocates RDMA devices to each role and enables Mooncake GPU
probability transfer. Workers advertise their Pod IP; networking must allow the
HTTP and dynamic Mooncake handshake ports. The controller permits these channels
between D/T Pods belonging to the same service. Mooncake selects among visible
HCAs; resource allocation alone does not prove NIC isolation. The image also needs
a compatible Mooncake wheel and GPU registration support.

Without an RDMA allocation, this example accepts temperature zero only. With RDMA,
requests may use `temperature`, `top_p`, `top_k` and a Target `seed`.

The role launcher enables vLLM batch invariance by default on NVIDIA and disables
it on MetaX. The NVIDIA mode requires compute capability 8.0 or newer and compatible engine backends;
see [role startup requirements](../../data-plane/dt-plugin/README.md#start-two-roles)
for the native environment override and numerical/performance limitations.

From the repository root, after installing that platform:

```bash
foretoken deploy examples/draft-target --timeout 20m
FRONTEND_URL="$(foretoken endpoint examples/draft-target)"
curl --fail-with-body "$FRONTEND_URL/v1/chat/completions" \
  -H 'Content-Type: application/json' \
  -d '{"model":"Qwen/Qwen3-4B","messages":[{"role":"user","content":"Hello"}],"temperature":0,"max_tokens":256}'
```

`spec.model` selects the main model. `spec.speculation.draftPool` names the Pool
that proposes candidates; it must set `modelPools[].model` to the Draft weights.
All other Pools use the main model and verify candidates. Both sides keep
`role: aggregate`. Ordinary Aggregate deployments omit `speculation` and the
Draft-only Pool.
Only the referenced Draft Pool may override the model. Both inherit the service's
model source and tokenizer; by default the tokenizer comes from the main model. Choose models with compatible token
IDs, not just similar names. For `source: local`, this initial deployment requires
absolute model and tokenizer paths visible inside the Pods.

Change each Pool's `replicas` independently and deploy again. The frontend groups
ready requests and selects DT workers each round. Draft can change between rounds;
existing Target sessions stay on their owner under the public scheduler. Explicit
Target migration is available through the standalone
[batch example](../../data-plane/dt-plugin/docs/frontend-workflow.md#move-a-request-between-targets).
New requests and queued requests without a Target session use the updated worker
inventory; requests with a Target session retain their runtime generation. Scaling down closes admission, withdraws the route, and waits for
existing sessions; the model-server supervisor also waits
for retained transfer artifacts within the configured drain deadline.
If a selected worker closes admission before a new session opens, the request
returns to the ready queue and excludes that worker from later placement. Existing
Target sessions and confirmed prefixes remain intact. If no compatible worker is
selectable, the request waits for worker updates or released capacity until its
deadline or cancellation. Accepting workers can also queue work in vLLM; they need
not be idle. Network failures and failed inference operations are not replayed.
Multiple Draft replicas provide capacity; they do not jointly propose a tree.

Current limits: text input, linear candidates, one GPU per role instance, eager
execution and synchronous local scheduling. There is no P/D composition,
multimodal input, KV offload, automatic Target migration, structured output, profiling or `min_p`.
Candidate IDs and descriptors use HTTP; full proposal distributions use Mooncake
between GPU workers. Draft randomness is independent of Target's seed.
Kubernetes RDMA networking, metric-driven autoscaling, stochastic quality
evaluation and public-API performance benchmarks remain unverified. Rollouts
without spare GPU capacity can temporarily make the service unavailable.

```bash
foretoken delete examples/draft-target
```
