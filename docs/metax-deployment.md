<!--
SPDX-License-Identifier: Apache-2.0
SPDX-FileCopyrightText: Copyright contributors to the Foretoken project
-->

# Deploy a model on MetaX GPUs

English | [简体中文](metax-deployment_zh.md)

Deploy a model on a MetaX-enabled Foretoken cluster and call it through an OpenAI-compatible HTTP API.

This guide uses `Qwen/Qwen3-0.6B`. If the cluster is not ready yet, its administrator should complete [MetaX platform setup](development/metax-platform.md) first.

## Before you start

You need the Foretoken CLI, kubectl, curl, cluster access supplied by your administrator, and a checkout of the Foretoken examples. Run commands from the repository root. If the CLI is not installed, follow the [CLI installation instructions](../cli/README.md#install-the-command-line-tool).

Confirm the following with the administrator:

- The platform has MetaX-compatible images, GPU resources, and storage for the model cache. The default example requests one GPU, 8 CPU, and 52 GiB memory.
- This guide uses the `foretoken-demo` namespace and Gateway access. Confirm that you may use that namespace and obtain a hostname assigned to this model service.

If the platform exposes services directly through a `LoadBalancer` instead of Gateway, omit the `hostname` setting below. The deployment and request commands remain the same.

## 1. Deploy the example model

Configure `examples/quickstart/cache.yaml` with the directory or StorageClass prepared by the administrator; see [Model storage](model-storage.md).

Add `hostname` under the existing `spec` in `examples/quickstart/frontend.yaml`. Replace the example hostname with the one assigned by your administrator and keep the other settings:

```yaml
spec:
  hostname: foretoken.example.com
```

Deploy the example:

```bash
foretoken deploy examples/quickstart --timeout 20m
```

The command exits when the model and frontend services are Ready.

To select another model, edit `examples/quickstart/model.yaml`. See the [single-model example](../examples/quickstart/README.md) for resources and cache settings. If you need a different namespace in a shared cluster, update both `namespace.yaml` and `kustomization.yaml`; changing only kubectl's default namespace does not change these manifests.

## 2. Send a request

Resolve the service URL and HTTP Host. Gateway uses the Host to route the request to the correct service:

```bash
FORETOKEN_FRONTEND_URL="$(foretoken endpoint examples/quickstart)"
FORETOKEN_REQUEST_HOST="$(foretoken endpoint examples/quickstart --host)"

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

## 3. Inspect or remove the deployment

```bash
foretoken status examples/quickstart
kubectl get pods --namespace foretoken-demo
```

When finished, remove the resources created by the same configuration:

```bash
foretoken delete examples/quickstart
```

The example includes its namespace, so deletion removes its services and PVC objects. Directory-backed model files remain available for reuse; dynamic volumes follow their storage retention policy. Use a namespace dedicated to the example. The administrator maintains the shared platform.

## If the request does not succeed

- **Deployment keeps waiting or a Pod is Pending:** run `kubectl describe pod --namespace foretoken-demo <pod-name>`. Share events about unavailable GPU, CPU, memory, or unbound cache volumes with the administrator.
- **HTTP 404:** check that the configured `hostname` matches the request Host. A `model_not_found` response instead means the model name is wrong; check `/v1/models`.
- **HTTP 503:** inspect `foretoken status` and Pod logs to confirm that the model loaded and the services are Ready before investigating the access endpoint.
