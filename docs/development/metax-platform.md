<!--
SPDX-License-Identifier: Apache-2.0
SPDX-FileCopyrightText: Copyright contributors to the Foretoken project
-->

# Prepare Foretoken for MetaX GPUs

English | [简体中文](metax-platform_zh.md)

Install Foretoken on a MetaX GPU cluster or build custom runtime images. For model deployment, see [Deploy and call a model](../metax-deployment.md).

## What the administrator provides

- Kubernetes 1.29 or later, MetaX drivers, and the MetaX device plugin publishing `metax-tech.com/gpu`.
- A writable model directory on the target nodes, or a StorageClass for model-cache volumes. Configure the example's `cache.yaml` as described in [Model storage](../model-storage.md).
- A reachable LoadBalancer or Gateway address.

The build host needs the Foretoken checkout, Docker with BuildKit, and Make. Platform installation also needs kubectl, Helm, and cluster permissions. Source installation downloads from GitHub, PyPI, the MetaX package index, and the selected container registries.

## Install release images

```bash
foretoken install
```

The MetaX image is selected automatically. See [CLI installation](../../cli/README.md#install-the-kubernetes-platform) for Gateway and custom configuration.

## Build the images

Use the following steps when a custom SDK or inference runtime is needed. Run commands from the Foretoken repository root.

### 1. Build the MetaX model-server image

Start with an Ubuntu 24.04 image containing the matching MACA SDK, or a Debian-based image with Python 3.12 and development headers. It does not need PyTorch or vLLM:

```bash
METAX_SDK_IMAGE=<maca-sdk-image> \
VLLM_METAX_VERSION=0.24.0 \
make image-model-server-metax
```

This produces `foretoken-vllm-metax:0.24.0` and `foretoken-model-server:dev`. Select matching SDK and driver versions from the [MetaX release matrix](https://vllm-metax.readthedocs.io/en/latest/getting_started/quickstart.html).

If a compatible MetaX vLLM image is already available, use it instead of the source build:

```bash
INFERENCE_ENGINE_IMAGE=<metax-vllm-image> \
FORETOKEN_VLLM_PYTHON=/opt/conda/bin/python \
make image-model-server
```

### 2. Build the controller and frontend images

```bash
make image-frontend
docker build -f control-plane/Dockerfile -t foretoken-control-plane:dev .
```

### 3. Push or import the images

Replace `<registry>/<project>` with a registry reachable by every target node:

```bash
export REGISTRY=<registry>/<project>
export MODEL_SERVER_IMAGE="$REGISTRY/foretoken-model-server:metax-v0.24.0"
export FRONTEND_IMAGE="$REGISTRY/foretoken-frontend:metax-v0.24.0"
export CONTROL_PLANE_IMAGE="$REGISTRY/foretoken-control-plane:metax-v0.24.0"

docker tag foretoken-model-server:dev "$MODEL_SERVER_IMAGE"
docker tag foretoken-frontend:dev "$FRONTEND_IMAGE"
docker tag foretoken-control-plane:dev "$CONTROL_PLANE_IMAGE"
docker push "$MODEL_SERVER_IMAGE"
docker push "$FRONTEND_IMAGE"
docker push "$CONTROL_PLANE_IMAGE"
```

For an offline cluster, import all three images into every node that may run a workload. See the [source image lifecycle guide](source-image-lifecycle.md#install-the-platform-with-helm).

## Install the platform

For manual Helm installation, prepare monitoring separately as described in [Observability](../../observability/README.md).

Create `metax-values.yaml` using the image names that were pushed or imported:

```yaml
image:
  repository: <registry>/<project>/foretoken-control-plane
  tag: metax-v0.24.0
frontend:
  enabled: true
  mode: gateway
  gateway:
    create: true
  image: <registry>/<project>/foretoken-frontend:metax-v0.24.0
runtime:
  vllm:
    image: <registry>/<project>/foretoken-model-server:metax-v0.24.0
    gpu:
      resourceName: metax-tech.com/gpu
      runtimeClassName: ""
```

If the registry is private, configure `imagePullSecrets` for the controller namespace and `workload.imagePullSecrets` for the model and frontend namespaces. The referenced Secrets must exist in their respective namespaces.

Install Envoy Gateway once if the cluster does not already provide one:

```bash
helm upgrade --install envoy-gateway \
  oci://docker.io/envoyproxy/gateway-helm \
  --namespace envoy-gateway-system \
  --create-namespace \
  --wait
```

Install the chart from the same checkout as the three images:

```bash
helm upgrade --install foretoken ./deploy/charts/foretoken \
  --namespace foretoken-platform \
  --create-namespace \
  --values metax-values.yaml \
  --wait

kubectl get pods --namespace foretoken-platform
kubectl get gateway --namespace foretoken-platform
```

The Gateway should have a reachable address and report `Programmed=True`. Continue with [Deploy and call a model](../metax-deployment.md).

If Gateway is not needed, set `frontend.mode: local` and `frontend.gateway.create: false`, and ensure that the cluster provides a reachable LoadBalancer address.

## Uninstall

Delete model deployments first, then use the command matching the installation method:

```bash
# CLI installation
foretoken uninstall

# Manual Helm installation
helm uninstall foretoken --namespace foretoken-platform
```

CRDs remain installed. Resources reused from the cluster are preserved.
