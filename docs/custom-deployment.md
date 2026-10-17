<!-- SPDX-License-Identifier: Apache-2.0 -->
<!-- SPDX-FileCopyrightText: Copyright contributors to the Foretoken project -->

# Deploy Foretoken from Source

[English](custom-deployment.md) | [中文](custom-deployment_zh.md)

Build Foretoken from a local checkout and deploy source changes to Kubernetes.

## Install from source

Prepare Python 3.11+, Git, kubectl and Helm, and select a GPU-enabled Kubernetes cluster. The cluster must allow source-build workloads and provide a default StorageClass for application files.

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

This installs the shared Foretoken platform; builds run in the cluster. It can also replace a published platform installed through the CLI. For k3d GPU setup, follow [Deploy Foretoken with k3d](k3d-deployment.md). Other Kubernetes clusters need a registry reachable by the builder and nodes; [Deploy Foretoken on Kubernetes](kubernetes-deployment.md#2-install-foretoken-from-source) covers the installation command and registry authentication.

Source builds automatically reuse persistent node-local compiler caches. To store build artifacts on another StorageClass, set `development.build.storageClassName` in `deploy/platform-values.yaml` and pass it with `--values`; execution and compiler caches remain on automatically managed local storage. Dynamically provisioned native NFS and `nfs.csi.k8s.io` volumes are supported. Application files use the cluster's default StorageClass, or `applicationFiles.storageClassName` when specified. Existing volumes keep their StorageClass.

## Deploy a model service

From the Foretoken repository root, deploy the Quick Start using the code in this directory:

```bash
foretoken deploy examples/quickstart/qwen3-0.6b --timeout 20m
```

This creates a frontend and a `Qwen/Qwen3-0.6B` model service in the `foretoken-demo` namespace, using one GPU. Once deployment completes, send a request:

```bash
FRONTEND_URL="$(foretoken endpoint examples/quickstart/qwen3-0.6b)"
curl --fail-with-body --no-buffer "$FRONTEND_URL/v1/chat/completions" \
  -H 'Content-Type: application/json' \
  -d '{"model":"Qwen/Qwen3-0.6B","messages":[{"role":"user","content":"Hello"}],"stream":true}'
```

After changing controller, frontend or model-server code, rerun `foretoken deploy`; it builds the changes and updates the deployment namespace. Use `foretoken install -e .` for shared CRD or platform configuration changes. Updating serving code restarts the affected processes and may reload model weights.

To deploy from another directory, add `-e` with the path to the Foretoken checkout, for example `foretoken deploy foretoken/examples/quickstart/qwen3-0.6b -e foretoken --timeout 20m` from its parent directory. The deployment path remains relative to the directory where the command runs.

CLI changes use the directory where `pip install -e .` was run. Reinstall the CLI when changing its Python dependencies.

Remove the example when finished:

```bash
foretoken delete examples/quickstart/qwen3-0.6b
```

### Run candidates in parallel

Give each candidate an independent Foretoken source directory and Kubernetes namespace to build and deploy different changes in parallel. Use separate clones or Git worktrees, which provide separate working directories from one repository. Candidates share the CLI installation and cluster platform, while each namespace has its own controller and serving code.

From the repository root, create a candidate and a Kustomize configuration based on the Quick Start:

```bash
git worktree add ../candidate-a -b experiment/candidate-a
cd ../candidate-a
mkdir -p examples/candidate
cat > examples/candidate/kustomization.yaml <<'EOF'
apiVersion: kustomize.config.k8s.io/v1beta1
kind: Kustomization
namespace: candidate-a
resources:
  - ../quickstart/qwen3-0.6b
patches:
  - target:
      kind: RuntimeCache
    patch: |-
      - op: replace
        path: /spec
        value:
          initialSize: 20Gi
          accessMode: ReadWriteOnce
          retentionPolicy: Delete
EOF

foretoken deploy examples/candidate --timeout 20m
foretoken perf examples/candidate --num-prompts 20 \
  --output experiment --output-dir results/candidates --iteration candidate-a
```

The candidate keeps the Quick Start's single-GPU configuration and gets 20 GiB of model storage from the cluster, so no host-directory mount is needed. To share an existing model directory instead, follow [model storage](model-storage.md).

Use separate source directories, namespaces and iteration names for other candidates. Each needs its own GPU allocation, CPU and memory. Add `spec.nodeSelector` when a candidate needs particular nodes; it does not select GPU indices.

Results are saved in each source directory. The [experiment records guide](../rsi-playbooks/experiment-records.md#analyze-results) explains which files to inspect when comparing candidates. Shared CPU, storage and network activity can affect measurements. With all GPUs occupied, replacement may briefly stop the old service to release its devices.

Delete the candidate when finished:

```bash
foretoken delete examples/candidate
```

Deletion waits for the candidate's services to finish cleanup, then removes its namespace controller and namespace. It also cleans up build resources no longer in use, while preserving source directories, experiment results and source archives.

Uninstall the platform only after all candidates have finished.

## Edit an inference engine

To use vLLM code from a local directory, pass that directory on deployment. It must match the installed runtime's Python, PyTorch and accelerator environment. For vLLM at `../vllm`:

```bash
foretoken deploy examples/quickstart/qwen3-0.6b --engine-source ../vllm --timeout 20m
```

Subsequent deployments from this Foretoken directory reuse the selected vLLM directory. After editing Python, Triton or NVIDIA CUDA/C++ code, rerun `foretoken deploy`; compilation is automatic. Select the engine directory separately for each Foretoken source directory.

For MetaX native kernels, also select the plugin directory alongside its matching vLLM directory:

```bash
foretoken deploy examples/quickstart/qwen3-0.6b \
  --engine-source ../vllm \
  --engine-source vllm-metax=../vllm-metax --timeout 20m
```

### Select a different runtime environment

A standard deployment does not require changing the base image. The default image includes the Python, PyTorch, CUDA, and vLLM components needed to run the model service.

To use a custom environment, edit `baseImage` in [`deploy/inference-engines/vllm/source-environment.json`](../deploy/inference-engines/vllm/source-environment.json):

```json
{"baseImage": "ghcr.io/example/custom-vllm:latest"}
```

Source installs, `make image-model-server`, `deploy/dev-build`, and release builds all use this setting. The image must be compatible with Foretoken's vLLM adapter and accessible to the builder. After changing it in an existing source installation, run `foretoken deploy` again.

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
