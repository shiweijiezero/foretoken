<!-- SPDX-License-Identifier: Apache-2.0 -->
<!-- SPDX-FileCopyrightText: Copyright contributors to the Foretoken project -->

# Deploy Foretoken from Source

[English](custom-deployment.md) | [中文](custom-deployment_zh.md)

This guide explains how to build and deploy Foretoken from source and redeploy source changes.

## 1. Prepare the target Kubernetes cluster

Confirm that `kubectl` points to the target cluster:

```bash
kubectl config current-context
kubectl get nodes
```

## 2. Build and deploy the source

Install the CLI from the source root with pip:

```bash
pip install -e .
```

Or create and activate a virtual environment with uv:

```bash
uv venv
source .venv/bin/activate
uv pip install -e .
```

Build and install the current source into the active Kubernetes context:

```bash
foretoken install -e .
```

A standard kind or k3d context builds and imports local images. Other Kubernetes contexts require a registry reachable by every target node:

```bash
foretoken install -e . --registry ghcr.io/example/foretoken
```

A private registry also requires the same pull Secret name in the platform namespace and every workload namespace. Reference it through a values file:

```yaml
imagePullSecrets:
  - name: registry-auth
workload:
  imagePullSecrets:
    - name: registry-auth
```

```bash
foretoken install -e . \
  --registry registry.example.com/foretoken \
  --values platform-values.yaml
```

The command reuses the repository's existing build, import, and push lifecycle before running the same platform and observability installation used for release images. It is the authoritative source installation path. The optional section below shows how to inspect local image imports while diagnosing that workflow; it does not define a second platform lifecycle.

### 2.1 Inspect local image imports

**Option 1: Import into a Kind cluster.** Create a Kind cluster directly to validate the control plane, CRDs, frontend, and scheduling behavior. To run a GPU model service, use k3d in option 2 and select the available GPUs as described in [Deploy Foretoken with k3d](k3d-deployment.md). Install Kind first:

```bash
export KIND_VERSION=v0.32.0
mkdir -p ./tmp/bin
curl -fL \
  -o ./tmp/bin/kind \
  "https://github.com/kubernetes-sigs/kind/releases/download/$KIND_VERSION/kind-linux-amd64"
chmod +x ./tmp/bin/kind
export PATH="$PWD/tmp/bin:$PATH"
kind version
```

Create a single-node cluster:

```bash
# Expected runtime: about 20 seconds
export KIND_CLUSTER=foretoken-local
kind create cluster --name "$KIND_CLUSTER"
```

If you need to simulate a multi-node topology on the same machine, use the Kind config included in the project.

```bash
# Expected runtime: about 30 seconds
export KIND_CLUSTER=foretoken-local
kind create cluster \
  --name "$KIND_CLUSTER" \
  --config deploy/kind/multi-node.yaml
```

After creating the cluster, build and import the local images.

```bash
# Expected runtime: about 8 minutes
make dev-build

kind load docker-image \
  --name "$KIND_CLUSTER" \
  foretoken-dev-control-plane:latest \
  foretoken-dev-frontend:latest \
  foretoken-dev-model-server:latest

mkdir -p ./tmp
kind get kubeconfig --name "$KIND_CLUSTER" \
  > "./tmp/kubeconfig-$KIND_CLUSTER.yaml"
export KUBECONFIG="$PWD/tmp/kubeconfig-$KIND_CLUSTER.yaml"
kubectl get nodes
```

**Option 2: Import into a k3d cluster.** List the clusters on the current machine. The following example uses the `foretoken-qwen-test` cluster created by the k3d deployment guide:

```bash
k3d cluster list
export CLUSTER=foretoken-qwen-test
```

If the target cluster has not been created, complete the cluster creation steps in [Deploy Foretoken with k3d](k3d-deployment.md) first. Then build and import the local images from the repository root.

```bash
# Expected runtime: about 6 minutes
make dev-build

k3d image import --cluster "$CLUSTER" \
  foretoken-dev-control-plane:latest \
  foretoken-dev-frontend:latest \
  foretoken-dev-model-server:latest

mkdir -p ./tmp
k3d kubeconfig get "$CLUSTER" \
  > "./tmp/kubeconfig-$CLUSTER.yaml"
export KUBECONFIG="$PWD/tmp/kubeconfig-$CLUSTER.yaml"
kubectl get nodes
```

## 3. Deploy the Quick Start (optional)

The Quick Start requires GPU resources in the target Kubernetes cluster. With k3d, first configure the GPUs as described in [Deploy Foretoken with k3d](k3d-deployment.md), then confirm that the current Kubernetes context points to the target k3d cluster.

To start the example frontend and `Qwen/Qwen3-0.6B` model service, deploy from the repository root using the CLI installed in section 2:

```bash
foretoken deploy examples/quickstart --timeout 6m
```

The command discovers the rendered services, reports state changes, and exits when the current configuration is ready.

## 4. Send a request (optional)

After completing [section 3: Deploy the Quick Start](#3-deploy-the-quick-start-optional), resolve the default `local` frontend URL:

```bash
FRONTEND_URL="$(foretoken endpoint examples/quickstart)"
```

Check the frontend and model routing first:

```bash
curl --fail "$FRONTEND_URL/healthz"
curl --fail "$FRONTEND_URL/v1/models"
```

After `/healthz` succeeds and `/v1/models` lists `Qwen/Qwen3-0.6B`, send an OpenAI-compatible request:

```bash
curl "$FRONTEND_URL/v1/chat/completions" \
  -H 'Content-Type: application/json' \
  -d '{
    "model": "Qwen/Qwen3-0.6B",
    "messages": [{"role": "user", "content": "Reply with: Foretoken is ready"}],
    "max_tokens": 32,
    "temperature": 0
  }'
printf '\n'
```

## 5. Redeploy source changes

Run the same source installation command after changing the code:

```bash
foretoken install -e .
```

For a remote cluster, keep using the same registry:

```bash
foretoken install -e . --registry ghcr.io/example/foretoken
```

BuildKit reuses compilation caches. The command imports or pushes only changed images, preserves the source installation mode, and rolls out workloads whose local image content changed.
