<!--
SPDX-License-Identifier: Apache-2.0
SPDX-FileCopyrightText: Copyright contributors to the Foretoken project
-->

# Profile an existing service

English | [简体中文](profiling_zh.md)

Collect a short execution timeline while an existing model service handles requests. This experimental feature requires a source installation and supports PyTorch Profiler or NVIDIA Nsight Systems with vLLM on NVIDIA GPUs.

## Capture

Use the Kustomize directory that identifies the deployed service:

```bash
foretoken profile examples/quickstart \
  --profile-engine pytorch \
  --profile-duration 15s
```

The command reads the directory to identify the deployed service without applying it. If it contains several models, select one with `--model MODEL_ID`. It does not generate traffic; send requests through the normal frontend while capture is running.

The selected ModelService must use persistent RuntimeCache storage. The maintained Quick Start already declares it in `cache.yaml`; other deployments can follow [Model storage](../docs/model-storage.md). Profiling writes to the `profiles/` directory on the same RuntimeCache PVC. A service without persistent RuntimeCache storage must be redeployed with one before capture.

The runtime starts the recording after profiler startup, stops after the requested duration, and then exports the files. Export may take longer than recording. Normal completion leaves the model serving. The command prints the ProfileRun name and, on completion, the RuntimeCache PVC and path containing the results.

| Option | Meaning |
|---|---|
| `--profile-engine pytorch` or `nsight` | Required profiler selection; must match the prepared service |
| `--profile-duration 15s` | Required recording duration; excludes startup and export |
| `--model MODEL_ID` | Select one model from a multi-model directory |
| `--timeout 10m` | How long the CLI observes the run, not how long the runtime records |

Ctrl-C requests cancellation and retains available output. After a lost terminal or observation timeout, capture still ends at its original deadline. Use the printed inspection command to check progress.

## Nsight Systems

Nsight Systems records CUDA and NVTX events across a runtime's engine process tree. It is an alternative to PyTorch capture, selected before model startup; changing the choice rolls out new model processes. This integration does not collect CPU sampling or GPU hardware counters and does not require privileged Pods.

### Prepare the diagnostic image

Build the source model-server image following [Source deployment](../docs/custom-deployment.md). Set `MODEL_SERVER_IMAGE` to that image and `NSIGHT_IMAGE` to a destination accessible to your cluster, then build the optional Linux x86_64 diagnostic image:

```bash
docker build -f deploy/inference-engines/nsight/Dockerfile \
  --build-arg MODEL_SERVER_IMAGE="$MODEL_SERVER_IMAGE" \
  -t "$NSIGHT_IMAGE" deploy/inference-engines/nsight
docker push "$NSIGHT_IMAGE"
```

The Dockerfile installs pinned Nsight Systems 2025.3.2 tooling. Add its image reference to your platform values:

```yaml
runtime:
  vllm:
    nsightImage: YOUR_NSIGHT_IMAGE
```

Replace `YOUR_NSIGHT_IMAGE` with the pushed image reference and apply the values with your source platform installation. Only models selecting Nsight use this image; ordinary services keep the configured normal runtime image.

### Deploy and capture

The maintained overlay selects `spec.profiling.engine: nsight` and persistent RuntimeCache storage:

```bash
foretoken deploy examples/nsight --timeout 20m
foretoken profile examples/nsight --profile-engine nsight --profile-duration 15s
```

Send traffic while the command reports `Capturing`. Nsight exports its report before completion, without stopping the model. Repeat the profile command for another independent capture. An existing PyTorch-prepared service must first be redeployed with `spec.profiling.engine: nsight`; a mismatched capture request is rejected before recording.

Open `capture.nsys-rep` in Nsight Systems, or summarize a copied report with `nsys stats capture.nsys-rep`. Successful captures also retain `capture.sqlite` for analysis. Nsight Compute kernel-counter profiling is a separate capability and is not included.

## Inspect results

Each runtime stores one manifest and its native report files (`.pt.trace.json` for PyTorch, `.nsys-rep` for Nsight) below:

```text
profiles/runs/<run-uid>/<runtime-id>/
```

The Quick Start examples store results under the repository-root `data/profiles/runs/`. For other deployments, use the PVC and relative path printed by the command.

Profiling adds CPU/GPU overhead and can produce large files even in a short window. Use a small diagnostic deployment and a short duration. A native profiler failure may terminate that runtime, so use a service where interruption is acceptable.
