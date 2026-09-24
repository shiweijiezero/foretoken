<!-- SPDX-License-Identifier: Apache-2.0 -->
<!-- SPDX-FileCopyrightText: Copyright contributors to the Foretoken project -->

# MiniMax H3 FL2VA BF16 on two NVIDIA A100 GPUs

English | [简体中文](README_zh.md)

Run one native-BF16 MiniMax H3 FL2VA model group on a single node with two
NVIDIA A100 80 GB GPUs. Foretoken manages the vLLM-Omni process lifecycle
through the thin `foretoken-omni-model-server` adapter and exposes the generated
ModelGroup Service directly.

The runtime image, Kubernetes deployment, H3 checkpoint loading, and a complete
FL2VA video request have been validated on two NVIDIA A100 80 GB GPUs. The
locally built vLLM-Omni image used for that run is not yet reproducible from a
published source revision; see the source requirement below.

## Prepare

### Build the H3 vLLM-Omni base image

The base image must contain the H3-compatible vLLM-Omni Python package and its
plugins. The validated local image was built from a checkout based on upstream
commit `a4ea67a21b20054dacc6e83952f9bd407e8ee4e7`, with additional local
changes. Those changes have not yet been published as a commit or patch set.
**The upstream commit alone does not reproduce the validated two-GPU image.**
Before building elsewhere, put the required H3 changes in a fixed, reviewable
source revision and use that exact checkout; do not rely on a developer's
Conda environment or an uncommitted working tree.

From the root of that clean H3-compatible vLLM-Omni checkout, build its CUDA
image. The vLLM-Omni Dockerfile installs the checkout into an image based on
vLLM 0.26.0; it does not copy packages from the host Python environment.
Record the source revision and image ID alongside the deployment:

```bash
git status --short
test -z "$(git status --porcelain)"
git rev-parse HEAD
DOCKER_BUILDKIT=1 docker build \
  --build-arg BASE_IMAGE=vllm/vllm-openai:v0.26.0 \
  -f docker/Dockerfile.cuda \
  -t vllm-omni-h3:bf16-tp2 .
docker run --rm --entrypoint python vllm-omni-h3:bf16-tp2 \
  -m pip show vllm vllm-omni
docker image inspect vllm-omni-h3:bf16-tp2 --format '{{.Id}}'
```

Use a clean checkout and an immutable base-image digest when exact rebuilds
matter. The upstream Dockerfile resolves Python dependencies during the build;
record or lock those dependencies as well if the same package set must be
recreated later. The fixed H3 source revision remains a prerequisite, not an
artifact supplied by this recipe today.

### Add the Foretoken adapter

From the Foretoken repository root, build the dedicated runtime image on top
of the H3 Omni image. This step adds the Foretoken adapter; it does not install
H3 or model weights. For a local k3d cluster, set `CLUSTER` as in the
[k3d guide](../../../../docs/k3d-deployment.md) and import the image:

```bash
make image-model-server-omni \
  INFERENCE_ENGINE_IMAGE=vllm-omni-h3:bf16-tp2 \
  OMNI_MODEL_SERVER_IMAGE=foretoken-omni-model-server:h3
k3d image import --cluster "$CLUSTER" foretoken-omni-model-server:h3
```

For a non-k3d cluster, tag and push this image to a registry reachable by the
nodes instead, and replace the local tag below with that image reference. Set
the image in the Foretoken platform values:

```yaml
runtime:
  vllmOmni:
    image: foretoken-omni-model-server:h3
```

Save those values as `platform-values.yaml`, then install or update Foretoken:

```bash
foretoken install -e . --values platform-values.yaml
```

### Prepare the FL2VA checkpoint

MiniMax H3 requires access to its Hugging Face model repository and the `hf`
CLI on the build workstation. Download the FL2VA files separately from the
image. The revision below is the one recorded for the validated checkpoint.
Choose an absolute data directory mounted on the target GPU node; the
[k3d guide](../../../../docs/k3d-deployment.md) mounts the repository's `data`
directory when creating the cluster:

```bash
mkdir -p data
DATA_ROOT="$(realpath data)"
hf auth login
hf download MiniMaxAI/MiniMax-H3 \
  --revision 42ed227ee7df40d41602854ae760620d6eb651fe \
  --include 'model_index.json' 'FL2VA/*' \
  --local-dir "$DATA_ROOT/models/MiniMax-H3"
test -f "$DATA_ROOT/models/MiniMax-H3/FL2VA/model_index.json"
```

Set `spec.directory` in this recipe's `cache.yaml` to the absolute value of
`DATA_ROOT`, not to the `models/MiniMax-H3/FL2VA` subdirectory. The checkpoint
will then be available at:

```text
<data-root>/models/MiniMax-H3/FL2VA/
```

If the k3d cluster already exists, confirm that its node has the same data
directory mounted before deploying. The checkpoint is not copied into either
Docker image. Keep Hugging Face credentials out of the model directory and
image build context.

See [model storage](../../../../docs/model-storage.md) for directory-backed and
dynamic storage options. The manifest requests two GPUs, 32 CPU cores and
256 GiB of host memory; adjust the CPU and memory values for the target node if
needed.

## Deploy and request

Run from the repository root:

```bash
RECIPE=examples/recipes/minimax-h3/a100-bf16-tp2
foretoken deploy "$RECIPE" --timeout 1h
kubectl -n foretoken-h3 get modelservices,modelpools,modelgroups,pods -w
```

The controller derives `num-gpus=2`, `tensor-parallel-size=2`, `usp=1`, and
`ring=1` from the requested GPU topology. The recipe does not create a
`FrontendService`: the current token frontend does not route video-generation
requests. Forward the generated ModelGroup Service instead:

```bash
SERVICE="$(
  kubectl -n foretoken-h3 get service \
    -l inference.foretoken.io/model-group \
    -o jsonpath='{.items[0].metadata.name}'
)"
kubectl -n foretoken-h3 port-forward "service/$SERVICE" 8091:9000
```

In another shell, submit a synchronous video request:

```bash
curl --fail-with-body \
  -X POST http://127.0.0.1:8091/v1/videos/sync \
  -F 'prompt=A cinematic tracking shot of a sailboat crossing a calm bay at sunrise.' \
  -F width=1024 \
  -F height=576 \
  -F num_frames=124 \
  -F fps=24 \
  -F num_inference_steps=50 \
  -F aspect_ratio=16:9 \
  -F flow_shift=12 \
  -F seed=1 \
  -F 'extra_params={"task":"fl2va","audio_flow_shift":3}' \
  --output h3-fl2va.mp4
```

The Omni image sets a 4000-second synchronous video request timeout; override
it with `OMNI_VIDEO_SYNC_TIMEOUT` when building the image. The separate
`timeouts.drain` value controls how long accepted requests may finish during
Pod shutdown.

Remove the workload with:

```bash
foretoken delete "$RECIPE" --timeout 10m
```

Checkpoint and runtime-cache files in the configured data directory remain.
