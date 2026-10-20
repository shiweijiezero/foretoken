<!--
SPDX-License-Identifier: Apache-2.0
SPDX-FileCopyrightText: Copyright contributors to the Foretoken project
-->

# Deploy a model on MetaX GPUs

English | [简体中文](metax_zh.md)

Deploy a model on a MetaX-enabled Foretoken cluster and call it through an OpenAI-compatible HTTP API.

This guide uses `Qwen/Qwen3-0.6B`. If the cluster is not ready yet, its administrator should complete [MetaX platform setup](metax-platform.md) first.

## Before you start

You need the Foretoken CLI, kubectl, curl, cluster access supplied by your administrator, and a checkout of the Foretoken examples. Run commands from the repository root. If the CLI is not installed, follow the [CLI installation instructions](../../cli/README.md#install-the-command-line-tool).

Confirm the following with the administrator:

- The platform has MetaX-compatible images, GPU resources, and storage for the model cache. The default example requests one GPU, 8 CPU, and 52 GiB memory.
- Your kubeconfig selects a namespace where you have service access.

The commands below work with direct LoadBalancer access and HTTP Gateway. Gateway routing assigns each instance a hostname automatically; the CLI resolves the required Host header.

## 1. Deploy the example model

The example uses the repository's `data/` directory. Once the administrator has prepared access to it, deploy directly. If the cluster uses different storage, update `examples/quickstart/qwen3-0.6b/cache.yaml` first using the [model storage guide](../features/model-storage.md). Custom domains and HTTPS or hostname-restricted listeners require [a matching hostname](../../README.md#gateway-mode).

```bash
foretoken deploy examples/quickstart/qwen3-0.6b --timeout 20m
```

The command exits when the model and frontend services are Ready.

Without `--name`, the instance name comes from `metadata.name` of the `FrontendService` in [`frontend.yaml`](../../examples/quickstart/qwen3-0.6b/frontend.yaml): `qwen3-0-6b` in this example. Add `--name candidate-a` to choose another instance name, then use `candidate-a` in the commands below. To select another model, edit `examples/quickstart/qwen3-0.6b/model.yaml`; resources and cache settings are described in the [single-model example](../../examples/quickstart/qwen3-0.6b/README.md).

## 2. Send a request

Resolve the service URL and HTTP Host. Gateway uses the Host to route the request to the correct service:

```bash
FORETOKEN_FRONTEND_URL="$(foretoken endpoint qwen3-0-6b)"
FORETOKEN_REQUEST_HOST="$(foretoken endpoint qwen3-0-6b --host)"

curl --fail-with-body --no-buffer \
  "$FORETOKEN_FRONTEND_URL/v1/chat/completions" \
  -H "Host: $FORETOKEN_REQUEST_HOST" \
  -H "Content-Type: application/json" \
  -d '{"model":"Qwen/Qwen3-0.6B","messages":[{"role":"user","content":"Hello"}],"stream":true}'
```

The response arrives in chunks and ends with `data: [DONE]`.

If you changed the model, update the request's `model` value as well. To list the names available from this service:

```bash
curl --fail-with-body "$FORETOKEN_FRONTEND_URL/v1/models" \
  -H "Host: $FORETOKEN_REQUEST_HOST"
```

For CPU/GPU execution traces, use [PyTorch profiling](../../benchmarks/docs/profile/README.md) with `--profile-engine pytorch`.

## 3. Inspect or remove the deployment

```bash
foretoken status qwen3-0-6b
foretoken status qwen3-0-6b --watch
```

When finished, remove the resources created by the same configuration:

```bash
foretoken delete qwen3-0-6b
```

Deletion removes only this instance and its controller, releasing its model-cache references after its workloads exit. Neighboring instances, the Kubernetes namespace and administrator-provided storage remain. See [Model storage](../features/model-storage.md#stop-delete-and-reuse) for cache collection and retained user files. The administrator maintains the shared platform.

## If the request does not succeed

- Deployment keeps waiting or a Pod is Pending: find the affected Pod in `foretoken status qwen3-0-6b`, then run `kubectl describe pod` with that Pod's name. Share events about unavailable GPU, CPU, memory, or unbound cache volumes with the administrator.
- HTTP 404: use the Host returned by `foretoken endpoint qwen3-0-6b --host`. A `model_not_found` response instead means the model name is wrong; check `/v1/models`.
- HTTP 503: inspect `foretoken status` and Pod logs to confirm that the model loaded and the services are Ready before investigating the access endpoint.
