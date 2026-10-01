<!-- SPDX-License-Identifier: Apache-2.0 -->
<!-- SPDX-FileCopyrightText: Copyright contributors to the Foretoken project -->

# Deploy Foretoken from Source

[English](custom-deployment.md) | [中文](custom-deployment_zh.md)

Build Foretoken in the target Kubernetes cluster and deploy changes from a local checkout. After installation, the same `foretoken deploy` command updates the code and deployment configuration.

## Install from source

Prepare Python 3.11+, Git, kubectl, and Helm. The cluster must allow BuildKit Pods and have a default StorageClass for persistent compiler caches. To choose another class, set `development.build.storageClassName` in a values file and pass it with `--values` when installing.

```bash
git clone https://github.com/shiweijiezero/foretoken.git
cd foretoken
pip install -e .
kubectl config current-context
```

For a local kind or k3d cluster, build and install without a registry:

```bash
foretoken install -e .
```

Builds run in dedicated Pods; images are loaded directly into the cluster nodes. Installation waits for the Kubernetes platform to become ready. For GPU setup in k3d, see [Deploy Foretoken with k3d](k3d-deployment.md).

### Remote clusters and private registries

Other clusters need a registry reachable by the build Pods and target nodes. Replace `example` with a namespace you can push to, and authorize the build using a Docker CLI login:

```bash
export REGISTRY=ghcr.io/example/foretoken
docker login ghcr.io
```

For a private registry, create an image pull Secret named `registry-auth` in `foretoken-platform` before installation, and in each workload namespace before deployment. Save these references in `platform-values.yaml`:

```yaml
imagePullSecrets:
  - name: registry-auth
workload:
  imagePullSecrets:
    - name: registry-auth
```

Install using that file:

```bash
foretoken install -e . --registry "$REGISTRY" --values platform-values.yaml
```

For publicly readable images, omit `--values` unless other overrides are needed. Registry login authorizes image pushes; the pull Secrets authorize cluster nodes to download private images.

## Deploy and update code

Deploy the maintained [Quick Start](../README.md#quick-start) on a GPU-enabled cluster:

```bash
foretoken deploy examples/quickstart --timeout 20m
```

After editing the checkout, run the same command again. It uses the saved installation settings and sends only added or changed files and deletions. Dedicated build Pods compile Rust changes and prepare Python updates; compiler caches and outputs stay in the cluster. With writable persistent runtime storage, these updates do not rebuild runtime images. Dependency, build, control-plane, and startup bootstrap changes use the image build path automatically.

Affected workloads restart and may reload model weights. The command waits for the selected code and serving routes to become active. Unchanged source and deployment configuration leave existing workloads running. Use the Quick Start's [request](../README.md#4-send-a-test-request) and [cleanup](../README.md#stop-and-uninstall) commands.

After changing installation settings in a values file, rerun installation with that file and the original registry and engine-source options before deploying. For source installations created before automatic updates were available, rerun the original installation command once to register the checkout.

## Edit an inference engine

To modify vLLM, bind a Git checkout matching the runtime's Python, PyTorch, and accelerator environment. For a checkout at `../vllm`:

```bash
foretoken install -e . --engine-source ../vllm
```

Retain `--registry` and `--values` when using them. After editing the engine checkout, use `foretoken deploy` as above. Python and Triton changes synchronize source; Triton JIT compilation runs in the inference engine. NVIDIA CUDA/C++ changes compile the vLLM extensions in the build Pod using persistent caches.

For MetaX, native kernels belong to the plugin checkout. Bind it alongside the matching core checkout:

```bash
foretoken install -e . \
  --engine-source ../vllm \
  --engine-source vllm-metax=../vllm-metax
```

The build Pod compiles plugin extensions for MetaX; core CUDA kernels are not used by that backend. Retain the installation's registry and values options.

### Select a different runtime environment

The runtime image supplies Python, PyTorch, and accelerator libraries. To change that environment, set a compatible image in `platform-values.yaml`, replacing the example with an image available to the cluster builder:

```yaml
runtime:
  vllm:
    image: ghcr.io/example/custom-vllm:latest
```

Reapply the installation command with `--values platform-values.yaml`, retaining the registry and engine-source options. With `-e`, Foretoken uses this image as its build base and adds the model-server. Then deploy the workload again.

vLLM-Omni uses a separate [build recipe and image setting](../examples/recipes/minimax-h3/a100-bf16-tp2/README.md#build-and-install). MetaX base-image builds are covered by [Prepare Foretoken for MetaX GPUs](development/metax-platform.md#install-from-source).
