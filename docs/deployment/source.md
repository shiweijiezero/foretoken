<!-- SPDX-License-Identifier: Apache-2.0 -->
<!-- SPDX-FileCopyrightText: Copyright contributors to the Foretoken project -->

# Deploy Foretoken from Source

[English](source.md) | [中文](source_zh.md)

Build Foretoken from a local source directory and deploy changes to Kubernetes. Ordinary folders, clones and worktrees use the same commands; build dependencies are prepared automatically from the versions declared with the source.

Prefer existing compatible images and build caches, then update application code within that environment. Ordinary source edits use incremental compilation and publication; missing environments or changes to their dependencies and build inputs can require new images.

## Install from source

Prepare kubectl and [Helm 4](../../cli/README.md#before-you-start), and select a GPU-enabled Kubernetes cluster with a default StorageClass. On a shared cluster, ask the administrator for [source-build access](../../cli/README.md#prepare-a-namespace-for-members).

```bash
git clone https://github.com/shiweijiezero/foretoken.git
cd foretoken
kubectl config current-context

uv venv --python 3.12 --seed  # With pip: python3 -m venv .venv (Python 3.11+)
source .venv/bin/activate
uv pip install -e .  # Or: pip install -e .
```

Keep the environment active for the commands below.

The platform needs one source installation for the cluster. If it is already installed from source, continue with [deployment](#deploy-a-model-service); another workstation only needs the CLI and a source checkout. An existing published platform needs the source installation step once before `deploy -e`; that preparation also reuses available images and build caches.

For the initial installation on a local kind or k3d cluster, build and install without a registry:

```bash
foretoken install -e .
```

This installs the shared Foretoken platform; builds run in the cluster. It can also replace a published platform installed through the CLI. For k3d GPU setup, follow [Deploy Foretoken with k3d](k3d.md). Other Kubernetes clusters need a registry reachable by the builder and nodes; [Deploy Foretoken on Kubernetes](kubernetes.md#2-install-foretoken-from-source) covers the installation command and registry authentication.

The first source installation prepares toolchains and runtime environments. Later deployments reuse available images, downloaded dependencies and compatible compiler caches, so code updates usually take less time. Each source directory keeps its own incremental build state. After fixing an installation or deployment failure, rerun the same command from the same checkout and cluster to reuse retained build work.

To store build artifacts on another StorageClass, set `development.build.storageClassName` in `deploy/platform-values.yaml` and pass it with `--values`; execution and compiler caches remain on automatically managed local storage. Dynamically provisioned native NFS and `nfs.csi.k8s.io` volumes are supported. Application files use the cluster's default StorageClass, or `applicationFiles.storageClassName` when specified. Existing volumes keep their StorageClass.

## Deploy a model service

From the Foretoken repository root, deploy the Quick Start using the code in this directory. If the platform has no default model storage, [select storage](kubernetes.md#3-deploy-and-update-code) first:

```bash
foretoken deploy examples/quickstart/qwen3-0.6b -e . --timeout 20m
```

Without `--name`, the instance name comes from `metadata.name` of the `FrontendService` in [`frontend.yaml`](../../examples/quickstart/qwen3-0.6b/frontend.yaml): `qwen3-0-6b` in this example. Add `--name candidate-a` to choose `candidate-a` instead, and use that name in subsequent commands.

The instance runs in your current kubeconfig namespace, with a frontend and a `Qwen/Qwen3-0.6B` model service using one GPU. Once deployment completes, send a request:

```bash
FRONTEND_URL="$(foretoken endpoint qwen3-0-6b)"
REQUEST_HOST="$(foretoken endpoint qwen3-0-6b --host)"
curl --fail-with-body --no-buffer "$FRONTEND_URL/v1/chat/completions" \
  -H "Host: $REQUEST_HOST" \
  -H 'Content-Type: application/json' \
  -d '{"model":"Qwen/Qwen3-0.6B","messages":[{"role":"user","content":"Hello"}],"stream":true}'
```

After editing Go, Rust or Python code in the controller, frontend or model server, rerun the deployment command above. It updates the selected instance and reuses unchanged runtime environments. Use `foretoken install -e .` for shared CRD or platform configuration changes. Serving-code updates restart affected processes and may reload model weights.

To deploy from another directory, add `-e` with the path to the Foretoken checkout, for example `foretoken deploy foretoken/examples/quickstart/qwen3-0.6b -e foretoken --timeout 20m` from its parent directory. The deployment path remains relative to the directory where the command runs.

CLI changes use the source directory selected during editable installation. Reinstall the CLI when changing its Python dependencies.

Remove the example when finished:

```bash
foretoken delete qwen3-0-6b
```

### Run candidates in parallel

Use a separate source checkout and instance name for each candidate. Candidates share one installed platform and can run in the same Kubernetes namespace, with independently updated controllers and serving code. The default Gateway provides one HTTP entry point for all candidates.

Candidates with matching environments reuse runtime and compiler images by reference; each keeps its own code and writable build state. The node's image cache is shared across these instances, so adding an agent does not require copying an image. A different node still needs to fetch any required image it does not already have.

From the repository root:

```bash
git worktree add ../candidate-a -b experiment/candidate-a
git worktree add ../candidate-b -b experiment/candidate-b

foretoken deploy ../candidate-a/examples/quickstart/qwen3-0.6b \
  --name candidate-a -e ../candidate-a --timeout 20m
foretoken deploy ../candidate-b/examples/quickstart/qwen3-0.6b \
  --name candidate-b -e ../candidate-b --timeout 20m

foretoken perf ../candidate-a/examples/quickstart/qwen3-0.6b \
  --name candidate-a --num-prompts 20 \
  --output experiment --output-dir results/candidates --iteration candidate-a
```

The candidates keep the Quick Start's single-GPU configuration. When default model storage is configured, ordinary folders, clones and worktrees reuse it; runtime state remains private to each instance. See [model storage](../features/model-storage.md) to select a custom directory or separate dynamic storage.

Each candidate needs its own GPU allocation, CPU and memory. Add `spec.nodeSelector` when a candidate needs particular nodes; it does not select GPU indices.

The benchmark saves its results under `results/candidates`. The [experiment records guide](../../rsi-playbooks/experiment-records.md#analyze-results) explains which files to inspect when comparing candidates. Shared CPU, storage and network activity can affect measurements. With all GPUs occupied, replacement may briefly stop the old service to release its devices.

Delete the candidates when finished:

```bash
foretoken delete candidate-a
foretoken delete candidate-b
```

Deleting one candidate removes only that instance and its controller, not the neighboring candidate or Kubernetes namespace. Source directories, incremental build state, experiment results and administrator-provided storage are retained. See the [CLI guide](../../cli/README.md#clean-up) for model-cache and dynamic-volume cleanup.

## Edit an inference engine

To use vLLM code from a local directory, pass that directory on deployment. It must match the installed runtime's Python, PyTorch and accelerator environment. For vLLM at `../vllm`:

```bash
foretoken deploy examples/quickstart/qwen3-0.6b --engine-source ../vllm --timeout 20m
```

Subsequent deployments from this Foretoken directory reuse the selected vLLM directory. After editing Python, Triton or NVIDIA CUDA/C++ code, deploy with `-e .` to build and apply the changes. Select the engine directory separately for each Foretoken source directory.

To select build concurrency, prefix a source installation or deployment command with `BUILD_JOBS=4`. Each Go, Rust or native-engine compilation and Python bytecode preparation then uses four workers.

For MetaX native kernels, also select the plugin directory alongside its matching vLLM directory:

```bash
foretoken deploy examples/quickstart/qwen3-0.6b \
  --engine-source ../vllm \
  --engine-source vllm-metax=../vllm-metax --timeout 20m
```

### Select a different runtime environment

A standard deployment does not require changing the base image. The default image includes the Python, PyTorch, CUDA, and vLLM components needed to run the model service.

To use a custom environment, edit `baseImage` in [`deploy/inference-engines/vllm/source-environment.json`](../../deploy/inference-engines/vllm/source-environment.json):

```json
{"baseImage": "ghcr.io/example/custom-vllm:latest"}
```

Source installs, `make image-model-server`, `deploy/dev-build`, and release builds all use this setting. The image must be compatible with Foretoken's vLLM adapter and accessible to the builder. After changing it in an existing source installation, redeploy with `-e .`.

To override the base for one installation instead, set `runtime.vllm.image` in `deploy/platform-values.yaml`:

```yaml
runtime:
  vllm:
    image: ghcr.io/example/custom-vllm:latest
```

Reapply the installation command with `--values deploy/platform-values.yaml`, retaining the registry and engine-source options. With `-e`, Foretoken uses this image as its build base. Set the field to `auto` to restore automatic runtime selection. Then deploy the workload again.

MetaX base-image builds are covered by [Prepare Foretoken for MetaX GPUs](metax-platform.md#install-from-source).

### vLLM-Omni runtime

vLLM-Omni uses a separate model-server image. Build it from the repository root on a machine with Docker BuildKit and Make:

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

For a remote cluster, use the `REGISTRY` configured in the [Kubernetes deployment guide](kubernetes.md). Log in first when that registry requires authentication:

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
