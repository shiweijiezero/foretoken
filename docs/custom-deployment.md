<!-- SPDX-License-Identifier: Apache-2.0 -->
<!-- SPDX-FileCopyrightText: Copyright contributors to the Foretoken project -->

# Deploy Foretoken from Source

[English](custom-deployment.md) | [中文](custom-deployment_zh.md)

Build Foretoken from a local checkout and deploy source changes to Kubernetes.

## Install from source

Prepare Python 3.11+, Git, kubectl, and Helm. The cluster must allow BuildKit Pods and publishing Jobs, with a default StorageClass for compiler caches and published application files. To override their storage classes, set `development.build.storageClassName` or `applicationFiles.storageClassName` in `deploy/platform-values.yaml` and pass it with `--values`.

```bash
git clone https://github.com/shiweijiezero/foretoken.git
cd foretoken
kubectl config current-context
```

Install the CLI from this checkout:

```bash
pip install -e .
```

For a local kind or k3d cluster, build and install without a registry:

```bash
foretoken install -e .
```

Builds run in the cluster. For GPU setup in k3d, see [Deploy Foretoken with k3d](k3d-deployment.md).

## Deploy and update code

Deploy the maintained [Quick Start](../README.md#quick-start) on a GPU-enabled cluster:

```bash
foretoken deploy examples/quickstart --timeout 20m
```

After editing the checkout, run the same command again. Code updates restart affected workloads and may reload model weights. Use the Quick Start's [request](../README.md#4-send-a-test-request) and [cleanup](../README.md#stop-and-uninstall) commands.

Changes to CLI Python files take effect directly from the editable checkout; rerun `pip install -e .` when its Python dependencies change.

## Edit an inference engine

To modify vLLM, bind a Git checkout matching the runtime's Python, PyTorch, and accelerator environment. For a checkout at `../vllm`:

```bash
foretoken install -e . --engine-source ../vllm
```

Retain `--registry` and `--values` when using them. After editing Python, Triton or NVIDIA CUDA/C++ source, use `foretoken deploy` as above; required compilation is automatic.

For MetaX, native kernels belong to the plugin checkout. Bind it alongside the matching core checkout:

```bash
foretoken install -e . \
  --engine-source ../vllm \
  --engine-source vllm-metax=../vllm-metax
```

### Select a different runtime environment

普通部署不需要修改基础镜像。Foretoken 默认使用 `vllm/vllm-openai:v0.31.0`，其中包含运行模型服务所需的 Python、PyTorch、CUDA 和 vLLM。

如果需要使用自定义环境，修改 [`deploy/inference-engines/vllm/source-environment.json`](../deploy/inference-engines/vllm/source-environment.json) 中的 `baseImage`：

```json
{"baseImage": "ghcr.io/example/custom-vllm:latest"}
```

源码安装、`make image-model-server`、`deploy/dev-build` 和发布构建都会使用这个设置。镜像必须与 Foretoken 的 vLLM 适配器兼容，并且构建器可以访问。已有源码安装修改后，重新执行 `foretoken deploy`。

To override the base for one installation instead, set `runtime.vllm.image` in `deploy/platform-values.yaml`:

```yaml
runtime:
  vllm:
    image: ghcr.io/example/custom-vllm:latest
```

Reapply the installation command with `--values deploy/platform-values.yaml`, retaining the registry and engine-source options. With `-e`, Foretoken uses this image as its build base. Set the field to `auto` to restore automatic runtime selection. Then deploy the workload again.

MetaX base-image builds are covered by [Prepare Foretoken for MetaX GPUs](development/metax-platform.md#install-from-source).

### vLLM-Omni runtime

vLLM-Omni uses a separate model-server image. Build it from the repository root on a machine with Docker BuildKit, Make, and a rustup-managed Rust toolchain:

```bash
make image-vllm-omni VLLM_OMNI_IMAGE=foretoken-vllm-omni:latest
make image-model-server-omni \
  INFERENCE_ENGINE_IMAGE=foretoken-vllm-omni:latest \
  OMNI_MODEL_SERVER_IMAGE=foretoken-omni-model-server:latest
```

For k3d, set `CLUSTER` to the existing cluster name and import the image:

```bash
CLUSTER=foretoken-qwen-test
k3d image import --cluster "$CLUSTER" foretoken-omni-model-server:latest
```

For a remote cluster, use the `REGISTRY` configured in the [Kubernetes deployment guide](kubernetes-deployment.md). Log in first when that registry requires authentication:

```bash
docker tag foretoken-omni-model-server:latest "$REGISTRY/omni-model-server:latest"
docker push "$REGISTRY/omni-model-server:latest"
```

Set `runtime.vllmOmni.image` in `deploy/platform-values.yaml` to the image the nodes can pull. This example uses the local image; for a registry, replace it with the full pushed reference:

```yaml
runtime:
  vllmOmni:
    image: foretoken-omni-model-server:latest
```

Apply the runtime setting:

```bash
foretoken install -e . --values deploy/platform-values.yaml
```

Retain `--registry "$REGISTRY"` for remote platform builds and any other installation options. The editable vLLM source path above targets the standard vLLM backend.

After installation, run `foretoken deploy` with the Kustomize directory for your Omni service. For code updates, rebuild and distribute the Omni image, then set `runtime.vllmOmni.image` to a new tag or digest reference. Reapply the installation command and deploy the same Kustomize directory again.
