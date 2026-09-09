<!-- SPDX-License-Identifier: Apache-2.0 -->
<!-- SPDX-FileCopyrightText: Copyright contributors to the Foretoken project -->

# Deploy Foretoken from Source

[English](custom-deployment.md) | [中文](custom-deployment_zh.md)

This guide explains how to build Foretoken images from source, configure the Kubernetes platform to use them, and redeploy source changes. Model services remain separate and are deployed with `foretoken deploy`.

Prepare Python 3.10+, Git, Docker with BuildKit, Make, kubectl, Helm, and a Rust toolchain managed by rustup. Run commands from the Foretoken repository root.

## 1. Prepare the target Kubernetes cluster

Confirm that `kubectl` points to the target cluster:

```bash
kubectl config current-context
kubectl get nodes
```

## 2. Build images and install the platform from source

Install the command-line tool from the source root with pip:

```bash
pip install -e .
```

Or create and activate a virtual environment with uv:

```bash
uv venv
source .venv/bin/activate
uv pip install -e .
```

For a local kind or k3d cluster, build and import the images directly:

```bash
foretoken install -e .
```

For other clusters, use a registry reachable by every target node. Replace `example` with a namespace you can push to:

```bash
export REGISTRY=ghcr.io/example/foretoken
docker login ghcr.io
foretoken install -e . --registry "$REGISTRY"
```

For a private registry, create a pull Secret with the same name in the platform namespace and each workload namespace. Save its references in `platform-values.yaml`:

```yaml
imagePullSecrets:
  - name: registry-auth
workload:
  imagePullSecrets:
    - name: registry-auth
```

```bash
foretoken install -e . \
  --registry "$REGISTRY" \
  --values platform-values.yaml
```

## 3. Confirm the platform deployment

`foretoken install -e .` waits for the Helm release and control-plane rollout. After it exits successfully, inspect the installed release and controller:

```bash
helm status foretoken --namespace foretoken-platform
kubectl get deployment foretoken-control-plane \
  --namespace foretoken-platform
```

The Deployment should report all desired replicas as Ready. Model workloads appear only after the next step.

## 4. Deploy the Quick Start (optional)

The Quick Start workload requests one GPU, 8 CPU, and 52 GiB memory; allow additional capacity for the platform. With k3d, first configure the GPUs as described in [Deploy Foretoken with k3d](k3d-deployment.md), then confirm that the current Kubernetes context points to the target k3d cluster.

To start the example frontend and `Qwen/Qwen3-0.6B` model service, deploy from the repository root using the command-line tool installed in section 2:

```bash
foretoken deploy examples/quickstart --timeout 20m
```

The command discovers the rendered services, reports state changes, and exits when the current configuration is ready.

## 5. Send a request (optional)

After completing [section 4: Deploy the Quick Start](#4-deploy-the-quick-start-optional), resolve the default `local` frontend URL and send an OpenAI-compatible request:

```bash
FRONTEND_URL="$(foretoken endpoint examples/quickstart)"

curl --fail-with-body "$FRONTEND_URL/v1/chat/completions" \
  -H 'Content-Type: application/json' \
  -d '{
    "model": "Qwen/Qwen3-0.6B",
    "messages": [{"role": "user", "content": "Reply with: Foretoken is ready"}],
    "max_tokens": 32,
    "temperature": 0
  }'
printf '\n'
```

## 6. Redeploy source changes

Run the same source installation command after changing the code:

```bash
foretoken install -e .
```

For a remote cluster, keep using the same registry:

```bash
foretoken install -e . --registry "$REGISTRY"
```

BuildKit reuses compilation caches. The command imports or pushes only changed images, preserves the source installation mode, and rolls out workloads whose local image content changed. For lower-level image and Helm diagnosis, see the maintainer [source image lifecycle guide](development/source-image-lifecycle.md).
