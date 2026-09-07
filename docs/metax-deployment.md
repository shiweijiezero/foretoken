<!--
SPDX-License-Identifier: Apache-2.0
SPDX-FileCopyrightText: Copyright contributors to the Foretoken project
-->

# Deploy Foretoken on MetaX GPUs

English | [简体中文](metax-deployment_zh.md)

Foretoken uses MetaX GPUs through the [`vLLM-metax`](https://github.com/MetaX-MACA/vLLM-metax) hardware plugin. On a machine with a compatible driver and MACA SDK, install public source into an independent uv environment for text inference, or package the same environment for Kubernetes. A prebuilt MetaX vLLM image is not required.

## Install vLLM in an independent uv environment

The host needs a MetaX C-series GPU, compatible driver and MACA SDK, Python 3.12 with development headers, C/C++ build tools, Bash, curl, tar, patch, and uv. An administrator provides the SDK and its system libraries, including libelf, libnuma, GLib, libpng, and libjpeg. Python dependencies come from PyPI and the [MetaX package index](https://repos.metax-tech.com/r/maca-pypi/simple/); source downloads also require access to GitHub.

The example selects vLLM-metax 0.24.0, MetaX PyTorch 2.10, and mcoplib 0.4.9. This release line uses MACA 3.8.2.x; select the driver and SDK using the [official release matrix](https://vllm-metax.readthedocs.io/en/latest/getting_started/quickstart.html).

Run from the Foretoken repository root, choosing an installation directory that does not already exist:

```bash
export MACA_PATH=/opt/maca
export UV_PYTHON=3.12
export VLLM_ENV="$PWD/.gpu_cache/metax-vllm-0.24.0"

bash deploy/inference-engines/vllm-metax/install.sh "$VLLM_ENV" 0.24.0
source "$VLLM_ENV/activate"
```

The installer downloads matching vLLM-metax and upstream vLLM tags, installs Python dependencies in `$VLLM_ENV/.venv`, and retains sources under `$VLLM_ENV/third_party`. It neither inherits system site-packages nor skips dependency resolution. The MetaX plugin and mcoplib provide native kernels; upstream vLLM is built with `VLLM_TARGET_DEVICE=empty` rather than installed from an NVIDIA CUDA wheel.

After a complete installation succeeds, check the environment and GPU:

```bash
uv pip check --python "$VLLM_ENV/.venv/bin/python"
python -c 'import torch, vllm; print(torch.__version__, vllm.__version__); print(torch.cuda.is_available())'
```

For standalone use, start the inference service directly:

```bash
vllm serve Qwen/Qwen3-0.6B
```

This environment runs on its installation host. Kubernetes Pods do not read a host venv; build an image for cluster deployment.

### Version scope

The validated independent source combination is 0.24.0. The installer backports [MetaX's XGrammar dependency correction](https://github.com/MetaX-MACA/vLLM-metax/commit/1331d8ad37da9a69fe1140b7759633d509b722a9); the plugin distribution includes `+foretoken.1` to distinguish it from the original release. Transformers 5.5.3, XGrammar 0.2.1, and TVM FFI 0.1.9 preserve text import and TileLang native API compatibility. Full dependency resolution and `uv pip check` still run.

This combination has been validated for text generation and JSON-constrained output, not audio inference: the published torchaudio 2.4.1 wheel has a load-time ABI mismatch with PyTorch 2.10. Foretoken's EngineCore adapter accepts vLLM 0.20–0.28; this does not mean every version has passed independent installation or GPU validation.

## Build a Kubernetes image

### Build from a MACA SDK image

Provide an Ubuntu/Debian image with a matching MACA SDK. It does not need PyTorch, mcoplib, or vLLM installed. Building requires Docker with BuildKit:

```bash
METAX_SDK_IMAGE=<maca-sdk-image> \
VLLM_METAX_VERSION=0.24.0 \
make image-model-server-metax
```

The image build runs the same `install.sh` and produces `foretoken-vllm-metax:0.24.0` and `foretoken-model-server:dev`. model-server uses `/opt/foretoken-vllm/.venv/bin/python` inside the image without mounting a host venv.

### Optional: reuse an existing MetaX vLLM image

When a working runtime image is already available, skip source and dependency installation and add only the Foretoken model-server:

```bash
INFERENCE_ENGINE_IMAGE=<metax-vllm-image> \
FORETOKEN_VLLM_PYTHON=/opt/conda/bin/python \
make image-model-server
```

Use the actual interpreter path that provides vLLM in the selected image. This also produces `foretoken-model-server:dev`.

## Distribute the image

Replace `<registry>/<project>` with a registry reachable by the GPU nodes:

```bash
export MODEL_SERVER_IMAGE=<registry>/<project>/foretoken-model-server:metax-v0.24.0

docker tag foretoken-model-server:dev "$MODEL_SERVER_IMAGE"
docker push "$MODEL_SERVER_IMAGE"
```

For offline clusters, a node administrator can import the image as described in the [source image lifecycle guide](development/source-image-lifecycle.md). `runtime.vllm.image` must match the exact imported image name and tag.

## Configure and install Foretoken

The cluster needs Kubernetes 1.29 or later, a default `StorageClass`, a MetaX device plugin publishing `metax-tech.com/gpu`, and mxExporter with a compatible `ServiceMonitor` covering the MetaX nodes. Foretoken discovers and reuses mxExporter rather than installing it; prepare monitoring using the [observability guide](../observability/README.md). The workstation needs the Foretoken CLI, kubectl, and Helm.

The maintained single-model example runs two frontend replicas and one model replica, requesting a total of one GPU, 8 CPU, and 52 GiB memory.

Create `metax-values.yaml`, using the complete image name published or imported above:

```yaml
runtime:
  vllm:
    image: <registry>/<project>/foretoken-model-server:metax-v0.24.0
    gpu:
      resourceName: metax-tech.com/gpu
      runtimeClassName: ""
```

For a private registry, create an image pull Secret in the workload namespace and configure `workload.imagePullSecrets`.

Install Envoy Gateway, then Foretoken:

```bash
helm upgrade --install envoy-gateway \
  oci://docker.io/envoyproxy/gateway-helm \
  --namespace envoy-gateway-system \
  --create-namespace \
  --wait

foretoken install --frontend-mode gateway --values metax-values.yaml
```

Reuse a shared platform configured by its administrator instead of installing it again.

## Deploy and send a request

Add a hostname under the existing `spec` in `examples/quickstart/frontend.yaml`:

```yaml
spec:
  hostname: foretoken.example.com
```

Deploy and inspect readiness:

```bash
foretoken deploy examples/quickstart
foretoken status examples/quickstart
kubectl get pods --namespace foretoken-demo --output wide
```

The example's `resources.requests.gpu.count: 1` maps to a `metax-tech.com/gpu` request. The device plugin injects the assigned devices; ordinary Pods do not need `privileged` or a mount of the host's complete `/dev`.

Resolve the endpoint and list model identifiers:

```bash
FORETOKEN_FRONTEND_URL="$(foretoken endpoint examples/quickstart)"
FORETOKEN_REQUEST_HOST="$(foretoken endpoint examples/quickstart --host)"

curl --fail-with-body "$FORETOKEN_FRONTEND_URL/v1/models" \
  -H "Host: $FORETOKEN_REQUEST_HOST"
```

The default example publishes `Qwen/Qwen3-0.6B`. If the model was changed, use the identifier returned by `/v1/models`:

```bash
curl --fail-with-body --no-buffer \
  "$FORETOKEN_FRONTEND_URL/v1/chat/completions" \
  -H "Host: $FORETOKEN_REQUEST_HOST" \
  -H "Content-Type: application/json" \
  -d '{"model":"Qwen/Qwen3-0.6B","messages":[{"role":"user","content":"Hello"}],"stream":true}'
```

A completed stream ends with `data: [DONE]`.

## Cleanup and troubleshooting

Delete the example with `foretoken delete examples/quickstart`. Only the platform owner should run `foretoken uninstall`. Local uv environments and images remain under the responsibility of their creator.

- **Dependency resolution fails:** check the release matrix and retain uv's original diagnostic. Do not skip dependencies or arbitrarily downgrade native libraries.
- **MACA libraries cannot load:** source the installation's `activate` script and check SDK/driver compatibility. Container devices and driver libraries must be provided by the device plugin or container runtime.
- **A Pod remains Pending:** use `kubectl describe pod` to check GPU and other resource availability.
- **`torch` or `vllm` cannot be imported:** inspect the environment selected by `FORETOKEN_VLLM_PYTHON`; a host interpreter path does not configure a Pod's environment.
- **Gateway 404 or `model_not_found`:** check the request Host and the model identifier returned by `/v1/models`, respectively.
- **Frontend 503:** check ModelGroup and model-server Pod readiness first.
