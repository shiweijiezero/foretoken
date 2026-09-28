<!--
SPDX-License-Identifier: Apache-2.0
SPDX-FileCopyrightText: Copyright contributors to the Foretoken project
-->

# Draft/Target deployment

[中文](README_zh.md)

Deploy the main model and a separate Draft model in independent Aggregate Pools,
discovered and selected by the normal frontend Router. This example needs two
GPUs, a shared RuntimeCache, and a
source-built platform with the [external-speculation engine extension](../../data-plane/dt-plugin/docs/mrv2-integration.md).
The released and repository-pinned vLLM engines do not provide that extension.
Set the platform's `runtime.vllm.image` to your model-server image containing it;
installing the Python DT package alone does not add the engine APIs. Both roles
need the extended engine for probability transfer.

With the platform's existing `rdma.resourceName` and `rdma.resourceCount` configured,
the controller allocates RDMA devices to each role and enables Mooncake GPU
probability transfer. Workers advertise their Pod IP; networking must allow the
HTTP and dynamic Mooncake handshake ports. The controller permits these channels
between D/T Pods belonging to the same service. Mooncake selects among visible
HCAs; resource allocation alone does not prove NIC isolation. The image also needs
a compatible Mooncake wheel and GPU registration support.

Without an RDMA allocation, this example accepts temperature zero only. With RDMA,
requests may use `temperature`, `top_p`, `top_k` and a Target `seed`.

The role launcher enables vLLM batch invariance by default on both roles. Use
NVIDIA GPUs with compute capability 8.0 or newer and compatible engine backends;
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

Change each Pool's `replicas` independently and deploy again. New requests choose
one available Draft and one Target. Scaling down closes admission, withdraws the
route, and waits for existing sessions; the model-server supervisor also waits
for retained transfer artifacts within the configured drain deadline.
Multiple Draft replicas provide capacity; they do not jointly propose a tree.

Current limits: text input, linear candidates, one GPU per role instance, eager
execution and synchronous local scheduling. There is no P/D composition,
multimodal input, KV offload/transfer, structured output, profiling or `min_p`.
Candidate IDs and descriptors use HTTP; full proposal distributions use Mooncake
between GPU workers. Draft randomness is independent of Target's seed.
Kubernetes RDMA networking, metric-driven autoscaling, stochastic quality
evaluation and public-API performance benchmarks remain unverified. Rollouts
without spare GPU capacity can temporarily make the service unavailable.

```bash
foretoken delete examples/draft-target
```
