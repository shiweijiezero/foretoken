<!-- SPDX-License-Identifier: Apache-2.0 -->
<!-- SPDX-FileCopyrightText: Copyright contributors to the Foretoken project -->

# MiniMax H3 BF16 on two NVIDIA A100 GPUs

English | [简体中文](README_zh.md)

Generate video from a reference image with MiniMax H3's FL2VA mode, or use
Ref2VA with a reference video. This recipe deploys one frontend and one BF16
model replica using tensor parallelism across two GPUs (TP=2). The model requests
two A100 80 GB GPUs, 32 CPU cores, and 256 GiB of host memory; allow additional
capacity for the platform.

## Build and install

Prepare a GPU-enabled Kubernetes cluster and the tools in the
[source deployment guide](../../../../docs/custom-deployment.md). For a local
cluster, follow the [k3d guide](../../../../docs/k3d-deployment.md).
Run from the Foretoken repository root:

```bash
make image-vllm-omni VLLM_OMNI_IMAGE=foretoken-vllm-omni:latest
make image-model-server-omni \
  INFERENCE_ENGINE_IMAGE=foretoken-vllm-omni:latest \
  OMNI_MODEL_SERVER_IMAGE=foretoken-omni-model-server:latest
```

For k3d, import the model-server image into the cluster selected by `CLUSTER`:

```bash
k3d image import --cluster "$CLUSTER" foretoken-omni-model-server:latest
```

For a remote cluster, tag and push that image to a registry reachable by its
nodes, and use the pushed image reference below. Save the following as
`platform-values.yaml`:

```yaml
runtime:
  vllmOmni:
    image: foretoken-omni-model-server:latest
```

Install or update the platform. Remote clusters also need `--registry` as
shown in the source deployment guide.

```bash
foretoken install -e . --values platform-values.yaml
```

## Deploy

The default model source is the public Hugging Face repository
`MiniMaxAI/MiniMax-H3`. Weights download automatically on first startup and share
the repository-root `data/` directory with the Quick Start examples. Local k3d
uses the data mount from its setup guide; for a remote cluster, set `directory`
in `cache.yaml` to a directory available on the target node, as described in
[model storage](../../../../docs/model-storage.md).

For Gateway access, set `spec.hostname` in this recipe's `frontend.yaml` and
install the platform in [Gateway mode](../../../../README.md#gateway-mode)
before deploying.

```bash
RECIPE=examples/recipes/minimax-h3/a100-bf16-tp2
foretoken deploy "$RECIPE" --timeout 1h
```

## Generate from an image

Set `REFERENCE_IMAGE` to an existing PNG file:

```bash
REFERENCE_IMAGE=/path/to/reference.png
ENDPOINT="$(foretoken endpoint "$RECIPE" --timeout 10m)"
REQUEST_HOST="$(foretoken endpoint "$RECIPE" --host --timeout 10m)"
mkdir -p ./data
curl --fail --max-time 4000 \
  "${ENDPOINT%/}/v1/videos/sync" \
  -H "Host: $REQUEST_HOST" \
  -F model=MiniMaxAI/MiniMax-H3 \
  -F 'prompt=A cinematic tracking shot of a sailboat crossing a calm bay at sunrise.' \
  -F "input_reference=@${REFERENCE_IMAGE};type=image/png" \
  -F width=1024 -F height=576 -F num_frames=124 -F fps=24 \
  -F num_inference_steps=50 -F aspect_ratio=16:9 -F flow_shift=12 -F seed=1 \
  -F 'extra_params={"task":"fl2va","audio_flow_shift":3}' \
  --output ./data/video.mp4
```

The generated video is saved to `./data/video.mp4`.

## Generate from a video

Change `spec.engineArgs.task-type` in `model.yaml` to `ref2va`. After active
requests finish, delete and redeploy to release the two GPUs before loading the
other mode. Set `REFERENCE_VIDEO` to an existing MP4 file:

```bash
REFERENCE_VIDEO=/path/to/reference.mp4
foretoken delete "$RECIPE" --timeout 2h
foretoken deploy "$RECIPE" --timeout 1h
ENDPOINT="$(foretoken endpoint "$RECIPE" --timeout 10m)"
REQUEST_HOST="$(foretoken endpoint "$RECIPE" --host --timeout 10m)"
mkdir -p ./data
curl --fail --max-time 4000 \
  "${ENDPOINT%/}/v1/videos/sync" \
  -H "Host: $REQUEST_HOST" \
  -F model=MiniMaxAI/MiniMax-H3 \
  -F 'prompt=Continue the scene shown in the reference video with a smooth camera movement.' \
  -F "input_references=@${REFERENCE_VIDEO};type=video/mp4" \
  -F width=1024 -F height=576 -F num_frames=124 -F fps=24 \
  -F num_inference_steps=50 -F aspect_ratio=16:9 -F flow_shift=12 -F seed=1 \
  -F 'extra_params={"task":"ref2va","audio_flow_shift":3}' \
  --output ./data/h3-ref2va.mp4
```

The response is saved to `./data/h3-ref2va.mp4`. To return to image input, change
`task-type` back to `fl2va` and repeat the delete/deploy commands before sending
an FL2VA request.

## Other model sources

To use ModelScope, change these fields in `model.yaml` and use
`MiniMax/MiniMax-H3` in the request's `model` field:

```yaml
spec:
  model: MiniMax/MiniMax-H3
  source: modelscope
```

For a Hugging Face-compatible mirror, add its URL to `platform-values.yaml`
and rerun the platform installation command before deploying the model:

```yaml
runtime:
  vllm:
    modelSource:
      endpoint: https://your-huggingface-compatible-mirror.example
```

Keep `runtime.vllmOmni.image` in the same file. This endpoint applies to
Hugging Face downloads. For offline weights, use `source: local` with the model
root or selected FL2VA/Ref2VA directory described in
[model sources](../../../../docs/model-sources.md), and use the same model
identifier in the request.

## Clean up

```bash
foretoken delete "$RECIPE" --timeout 2h
```

The shared data directory is retained for later deployments.
