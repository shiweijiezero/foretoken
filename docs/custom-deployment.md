<!-- SPDX-License-Identifier: Apache-2.0 -->
<!-- SPDX-FileCopyrightText: Copyright contributors to the Foretoken project -->

# Deploy Foretoken from Source

[English](custom-deployment.md) | [中文](custom-deployment_zh.md)

This guide explains how to build Foretoken images from source, configure the Kubernetes platform to use them, and redeploy source changes. Model services remain separate and are deployed with `foretoken deploy`.

Use Python 3.10 or later. Run every command from the Foretoken repository root unless it says otherwise.

## 1. Prepare the target Kubernetes cluster

Confirm that `kubectl` points to the target cluster:

```bash
kubectl config current-context
kubectl get nodes
```

## 2. Build images and install the platform from source

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

Build Foretoken images from the current source and configure the platform to use them in the active Kubernetes context:

```bash
foretoken install -e .
```

A standard kind or k3d context builds and imports local images. Other Kubernetes contexts need a registry reachable by every target node. Before the first source installation, sign in to its registry host with an account that can push the target repository:

```bash
docker login ghcr.io
foretoken install -e . --registry ghcr.io/example/foretoken
```

Registry login authorizes the local image push. A private registry also needs the same pull Secret name in the platform namespace and every workload namespace so nodes can pull the images. Reference it through a values file:

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

The command reuses the repository's build, import, and push lifecycle before running the same platform and observability installation used for release images. This is the complete, recommended source installation path; continue with [section 3](#3-confirm-the-platform-deployment) after it succeeds.

For lower-level image import and raw Helm diagnosis, see the maintainer [source image lifecycle guide](development/source-image-lifecycle.md).

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

To start the example frontend and `Qwen/Qwen3-0.6B` model service, deploy from the repository root using the CLI installed in section 2:

```bash
foretoken deploy examples/quickstart --timeout 6m
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
