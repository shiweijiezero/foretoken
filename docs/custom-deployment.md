<!-- SPDX-License-Identifier: Apache-2.0 -->
<!-- SPDX-FileCopyrightText: Copyright contributors to the Foretoken project -->

# Deploy Foretoken from Custom Source

[English](custom-deployment.md) | [中文](custom-deployment_zh.md)

Use this guide when you want to build and deploy your modified Foretoken source to Kubernetes. The only deployment entry point is `make dev-deploy`.

For a k3d cluster setup and GPU prerequisites, see [Deploy Foretoken with k3d](k3d-deployment.md).

## Deploy

Run commands from the repository root.

For a first k3d deployment, this creates `foretoken-qwen-test` with GPU 1, builds the images, and deploys the Quick Start:

```bash
CLUSTER=foretoken-qwen-test GPU_INDICES=1 make dev-deploy
```

When the current Kubernetes context is an existing k3d cluster, deploy directly:

```bash
make dev-deploy
```

For a standard Kubernetes cluster, provide an OCI registry that the cluster can pull from:

```bash
REGISTRY=registry.example.com/foretoken make dev-deploy
```

For a private registry, run `docker login`, create the same image pull Secret in the platform and workload namespaces, and pass its name with `IMAGE_PULL_SECRET`.

## What the command does

`make dev-deploy` uses BuildKit to build the control-plane, frontend, and model-server images. BuildKit reuses its cache when build inputs have not changed. The script compares the local image IDs before and after each build; it does not inspect Git changes.

- With k3d, it imports only images whose local image ID changed. The large model-server image is therefore imported only when its build result changes.
- With `REGISTRY`, it pushes only changed images. Each pushed image receives a deployment tag, and the Helm upgrade automatically uses that tag. The model-server image is pushed only when its build result changes.
- It upgrades the local Helm chart, rolls out changed components, applies the Quick Start, and waits for its frontend and model service to become Ready.
- With `DEV_SMOKE=true` in local frontend mode, it sends a real OpenAI-compatible chat completion request after the Quick Start is ready.

## Optional variables

| Variable | Purpose |
| --- | --- |
| `CLUSTER` | Target k3d cluster. If it does not exist, `GPU_INDICES` is required to create it. When omitted, an existing current `k3d-*` context is used. |
| `GPU_INDICES` | Comma-separated host GPU indices exposed when creating a k3d cluster. |
| `REGISTRY` | OCI repository prefix for a standard Kubernetes deployment, for example `registry.example.com/foretoken`. |
| `FRONTEND_MODE` | Frontend mode: `local` (default) or `gateway`. Gateway mode creates the chart's gateway configuration. |
| `FRONTEND_HOSTNAME` | Public hostname patched into the default Quick Start when `FRONTEND_MODE=gateway`. |
| `INFERENCE_ENGINE_IMAGE` | Base inference-engine image for the model-server build. Use this when your source requires a different compatible runtime image. |
| `IMAGE_PULL_SECRET` | Name of the image pull Secret used by the control-plane and workload namespaces. |
| `DEV_EXAMPLE` | Kustomize path to apply after the chart upgrade. Defaults to `examples/quickstart`; that path also receives the built-in readiness wait. Set it empty to skip applying an example. |
| `DEV_TIMEOUT` | Helm, rollout, and readiness timeout. Defaults to `15m`. |
| `DEV_SMOKE` | Set to `true` to send a real request after a local-mode deployment. Defaults to `false`. |
| `TAG` | Local development image tag. Defaults to `latest`; choose a different value to keep separate local image sets. |

For example, deploy and then send a real local-mode request:

```bash
DEV_SMOKE=true make dev-deploy
```
