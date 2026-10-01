<!-- SPDX-License-Identifier: Apache-2.0 -->
<!-- SPDX-FileCopyrightText: Copyright contributors to the Foretoken project -->

# Deploy Foretoken from Source

[English](custom-deployment.md) | [中文](custom-deployment_zh.md)

This guide explains how to build Foretoken images from source, configure the Kubernetes platform to use them, and redeploy source changes. Model services remain separate and are deployed with `foretoken deploy`.

Prepare Python 3.11+, Git, Docker with BuildKit, Make, kubectl, Helm, and a Rust toolchain managed by rustup. Get the current source and run commands from its root:

```bash
git clone https://github.com/shiweijiezero/foretoken.git
cd foretoken
```

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

Configure the corresponding endpoint or proxy address when a mirror is required.

Distribute the built images through a registry reachable by every target node. Replace `example` with a namespace you can push to:

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

For a local kind or k3d cluster, you can omit `--registry` to import images directly:

```bash
foretoken install -e .
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

Source installation binds the checkout on this workstation to the target cluster. After editing it, run the deployment command again:

```bash
foretoken deploy examples/quickstart --timeout 20m
```

Deployment reuses the saved installation settings. Foretoken frontend and vLLM model-server Rust/Python changes use compiled executables or copied source without rebuilding runtime images when persistent runtime storage is available. Changes to dependencies, build configuration, the control plane, or the startup bootstrap use the image build path automatically. If source and deployment configuration are unchanged, existing workloads remain running.

Updated workloads restart and may reload model weights. The command waits for the selected code to be active and the services to be ready; use the request in section 5 to try the change.

After changing installation settings in a values file, rerun the installation command with that file and the same registry options before deploying. For source installations created before automatic updates were available, rerun the original installation command once to register the checkout.

## 7. Use a custom inference engine

Changes to vLLM itself, including its Python and CUDA kernels, require an inference-engine image containing those changes. Build that image using the engine's build instructions, then set its reference in `platform-values.yaml`. Replace the example reference with the image available to the local Docker builder:

```yaml
runtime:
  vllm:
    image: ghcr.io/example/custom-vllm:latest
```

With `-e`, this image is the build base: Foretoken adds its model-server and distributes the resulting image. For the registry setup in section 2, run:

```bash
foretoken install -e . --registry "$REGISTRY" --values platform-values.yaml
foretoken deploy examples/quickstart --timeout 20m
```

Omit `--registry` for local kind or k3d image import. Keep other installation overrides in the values file.

vLLM-Omni has a separate build and image setting; follow the [vLLM-Omni recipe](../examples/recipes/minimax-h3/a100-bf16-tp2/README.md#build-and-install). MetaX engine builds are covered by [Prepare Foretoken for MetaX GPUs](development/metax-platform.md#install-from-source).
