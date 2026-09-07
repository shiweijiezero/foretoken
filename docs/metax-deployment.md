<!--
SPDX-License-Identifier: Apache-2.0
SPDX-FileCopyrightText: Copyright contributors to the Foretoken project
-->

# Deploy Foretoken on MetaX GPUs

English | [简体中文](metax-deployment_zh.md)

Foretoken runs vLLM workloads on MetaX C-series GPUs through the [`vLLM-metax`](https://github.com/MetaX-MACA/vLLM-metax) hardware plugin. This guide builds a MetaX-compatible Foretoken model-server image, configures the Kubernetes GPU resource, deploys the maintained single-model example, and sends a request through Kubernetes Gateway.

## Prerequisites

Prepare:

- a Linux Kubernetes 1.29 or later cluster with a default `StorageClass` that supports volume expansion;
- MetaX C-series GPU nodes with the MetaX device plugin publishing `metax-tech.com/gpu`;
- the official MetaX mxExporter and a compatible `ServiceMonitor` covering every MetaX GPU node;
- `docker` with BuildKit, `kubectl`, Helm, and the Foretoken CLI;
- a released MetaX vLLM image for the selected version;
- an OCI registry reachable by the GPU nodes, or permission to import images into their container runtime.

The maintained Quick Start deploys two frontend replicas and one model replica; together they request one GPU, 8 CPU, and 52 GiB memory. Confirm that the cluster has this capacity before deploying.

MetaX releases vLLM, MACA, PyTorch, and mcoplib as a tested combination. Select the base image from the [vLLM-MetaX release matrix](https://vllm-metax.readthedocs.io/en/latest/getting_started/quickstart.html); do not combine packages from different rows. Foretoken discovers the cluster-provided mxExporter during installation and does not install it; see [Observability](../observability/README.md) for the discovery contract.

## Build the model-server image

Run commands from the Foretoken repository root. Choose either the released-image path or the public-source path.

### Use a released MetaX vLLM image

Set `INFERENCE_ENGINE_IMAGE` to the image selected from the MetaX release matrix. The image owns MACA, PyTorch, mcoplib, native libraries, and vLLM. Foretoken adds its model-server process without replacing that runtime:

```bash
INFERENCE_ENGINE_IMAGE=<matching-metax-vllm-image> \
FORETOKEN_VLLM_PYTHON=/opt/conda/bin/python \
make image-model-server
```

The result is `foretoken-model-server:dev`.

If the selected image exposes vLLM through a different Python executable, set `FORETOKEN_VLLM_PYTHON` to that absolute path.

### Build a uv-managed public-source overlay

For source development, Foretoken can create a virtual environment inside the image and install matching public tags of `vLLM-metax` and upstream vLLM. The build host must be able to reach GitHub, PyPI, and the MetaX Python package index:

```bash
METAX_BASE_IMAGE=<matching-metax-vllm-image> \
VLLM_METAX_VERSION=0.24.0 \
make image-model-server-metax
```

This creates:

```text
foretoken-vllm-metax:0.24.0
foretoken-model-server:dev
```

The base image continues to own the tested MACA, PyTorch, mcoplib, and native ABI. The `/opt/foretoken-vllm` environment only overlays the selected public `vX.Y.Z` tags. One `VLLM_METAX_VERSION` selects both repositories so their versions cannot drift.

The public MetaX source path currently supports vLLM-MetaX 0.20 through 0.24. Foretoken's EngineCore adapter accepts vLLM 0.20 through 0.28; later versions require an explicit compatibility update.

## Distribute the image

For a cluster that pulls from an OCI registry:

```bash
export REGISTRY=<registry>/<project>
export MODEL_SERVER_IMAGE="$REGISTRY/foretoken-model-server:metax-v0.24.0"

docker tag foretoken-model-server:dev "$MODEL_SERVER_IMAGE"
docker push "$MODEL_SERVER_IMAGE"
```

For an offline cluster, save the image and ask a node administrator to import it into every GPU node that may run the workload:

```bash
docker save foretoken-model-server:dev \
  --output foretoken-model-server-metax.tar

sudo ctr --namespace k8s.io images import \
  foretoken-model-server-metax.tar
```

Use a task-specific tag. Do not overwrite an image tag already used by another deployment.

## Configure and install Foretoken

Create `metax-values.yaml`:

```yaml
runtime:
  vllm:
    image: <registry>/<project>/foretoken-model-server:metax-v0.24.0
    gpu:
      resourceName: metax-tech.com/gpu
      runtimeClassName: ""
```

If the registry requires authentication, create an image pull Secret in each workload namespace and list its name under `workload.imagePullSecrets`.

Install Envoy Gateway, then install Foretoken in Gateway mode:

```bash
helm upgrade --install envoy-gateway \
  oci://docker.io/envoyproxy/gateway-helm \
  --namespace envoy-gateway-system \
  --create-namespace \
  --wait

foretoken install \
  --frontend-mode gateway \
  --values metax-values.yaml
```

A platform already managed by an administrator should be reused instead of installed again. In that case, confirm that its vLLM runtime image and GPU resource are configured for MetaX.

## Deploy a model

Add a hostname to `examples/quickstart/frontend.yaml`:

```yaml
spec:
  hostname: foretoken.example.com
```

Deploy the maintained example and wait for readiness:

```bash
foretoken deploy examples/quickstart
foretoken status examples/quickstart

kubectl get frontendservice,modelservice,modelpool,modelgroup \
  --namespace foretoken-demo
kubectl get pods --namespace foretoken-demo --output wide
```

The example requests one accelerator through `resources.requests.gpu.count`. The control plane maps that request to the platform's `metax-tech.com/gpu` resource. Do not add `privileged` or mount the host's complete `/dev`; the MetaX device plugin owns device injection.

## Send a request

Resolve the Gateway URL and required HTTP host:

```bash
export FORETOKEN_FRONTEND_URL="$(foretoken endpoint examples/quickstart)"
export FORETOKEN_REQUEST_HOST="$(foretoken endpoint examples/quickstart --host)"
```

List the model identifiers published by the frontend:

```bash
curl --fail-with-body \
  "$FORETOKEN_FRONTEND_URL/v1/models" \
  -H "Host: $FORETOKEN_REQUEST_HOST"
```

Use the returned model ID in a streaming request:

```bash
curl --fail-with-body --no-buffer \
  "$FORETOKEN_FRONTEND_URL/v1/chat/completions" \
  -H "Host: $FORETOKEN_REQUEST_HOST" \
  -H "Content-Type: application/json" \
  -d '{
    "model": "Qwen/Qwen3-0.6B",
    "messages": [{"role": "user", "content": "Hello"}],
    "stream": true
  }'
```

A successful stream ends with `data: [DONE]`.

## Clean up

Delete the example resources:

```bash
foretoken delete examples/quickstart
```

Run `foretoken uninstall` only when you own the platform installation. It must not be used to remove a shared platform or a Gateway managed by another team.

## Troubleshooting

- **The Pod cannot import `torch` or `vllm`:** verify that `FORETOKEN_VLLM_PYTHON` points to the Python executable inside the selected image. The released-image path commonly uses `/opt/conda/bin/python`; the uv overlay uses `/opt/foretoken-vllm/bin/python` automatically.
- **The Pod remains Pending:** run `kubectl describe pod` and confirm that the target node advertises available `metax-tech.com/gpu` capacity.
- **Engine startup rejects the version:** keep vLLM and `vLLM-metax` on the same release and within the supported range.
- **Gateway returns 404:** confirm that the `HTTPRoute` is accepted and that the request sends the hostname returned by `foretoken endpoint --host`.
- **The API returns `model_not_found`:** use the exact identifier returned by `/v1/models`.
- **The frontend returns 503:** check that the `ModelGroup` and model-server Pod are Ready before changing Gateway configuration.
