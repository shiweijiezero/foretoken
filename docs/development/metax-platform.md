<!--
SPDX-License-Identifier: Apache-2.0
SPDX-FileCopyrightText: Copyright contributors to the Foretoken project
-->

# Prepare Foretoken for MetaX GPUs

English | [简体中文](metax-platform_zh.md)

This guide is for the platform administrator who prepares MetaX images and the Foretoken platform. After this one-time setup, model users can follow [Deploy and call a model](../metax-deployment.md) without installing or understanding the inference engine.

Foretoken uses three images: the controller manages Kubernetes model services, the frontend receives requests, and model-server executes models on MetaX GPUs. Build all three from the same checkout and install the matching Helm chart so its APIs and CRDs match the examples.

## What the administrator provides

- Kubernetes 1.29 or later, MetaX drivers, and the MetaX device plugin publishing `metax-tech.com/gpu`.
- A writable model directory on the target nodes, or a StorageClass for model-cache volumes. Configure the example's `cache.yaml` as described in [Model storage](../model-storage.md).
- A reachable Gateway endpoint. The commands below use Envoy Gateway; an existing platform should remain under its current owner's control.
- Prometheus, Prometheus Operator, `ServiceMonitor`/`PrometheusRule` CRDs, and mxExporter when monitoring is required. See [Observability](../../observability/README.md).

The build host needs the Foretoken checkout, Docker with BuildKit, and Make. Platform installation also needs kubectl, Helm, and cluster permissions. Source installation downloads from GitHub, PyPI, the MetaX package index, and the selected container registries.

## Build the images

Run these commands from the Foretoken repository root.

### 1. Build the MetaX model-server image

Start with an Ubuntu 24.04 image containing the matching MACA SDK, or a Debian-based image with Python 3.12 and development headers. It does not need PyTorch or vLLM:

```bash
METAX_SDK_IMAGE=<maca-sdk-image> \
VLLM_METAX_VERSION=0.24.0 \
make image-model-server-metax
```

The build creates an isolated uv environment, installs matching public source tags, and produces `foretoken-vllm-metax:0.24.0` and `foretoken-model-server:dev`. The Pod uses the Python environment inside the image. Select the SDK and driver from the [MetaX release matrix](https://vllm-metax.readthedocs.io/en/latest/getting_started/quickstart.html).

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

The controller initializes the matching CRDs before starting. The chart creates the Gateway used by model services; it should report `Programmed=True` and have a reachable address. Give model users their cluster access, namespace, model configuration, and assigned hostname, then direct them to [Deploy and call a model](../metax-deployment.md).

If Gateway is not needed, set `frontend.mode: local` and `frontend.gateway.create: false`, and ensure that the cluster provides a reachable LoadBalancer address.

## Uninstall

Users should delete their model deployments first. Then the platform owner can run:

```bash
helm uninstall foretoken --namespace foretoken-platform
```

CRDs remain installed. RuntimeCache retention controls cache PVC cleanup. Envoy Gateway, monitoring, and images remain with their respective owners.
