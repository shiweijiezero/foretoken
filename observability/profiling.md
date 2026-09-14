<!--
SPDX-License-Identifier: Apache-2.0
SPDX-FileCopyrightText: Copyright contributors to the Foretoken project
-->

# Profile an existing service

English | [简体中文](profiling_zh.md)

Collect a short PyTorch CPU/GPU timeline while an existing model service handles requests. This experimental feature requires a source installation and currently supports the vLLM PyTorch profiler on NVIDIA GPUs.

## Capture

Use the Kustomize directory that identifies the deployed service:

```bash
foretoken profile examples/quickstart \
  --profile-engine pytorch \
  --profile-duration 15s
```

The command reads the directory without applying it. If it contains several models, select one with `--model MODEL_ID`. It does not generate traffic; send requests through the normal frontend while capture is running.

The selected ModelService must use persistent RuntimeCache storage. The maintained Quick Start already declares it in `cache.yaml`; other deployments can follow [Model storage](../docs/model-storage.md). Profiling writes to the `profiles/` directory on the same RuntimeCache PVC. A service without persistent RuntimeCache storage must be redeployed with one before capture.

The runtime stops recording after the requested duration and then exports the files. Export may take longer than recording. Normal completion leaves the model serving. The command prints a ProfileRun name for later inspection and the RuntimeCache PVC and path containing the results; it does not download them.

| Option | Meaning |
|---|---|
| `--profile-engine pytorch` | Required profiler selection; only PyTorch is available |
| `--profile-duration 15s` | Required recording duration; excludes startup and export |
| `--model MODEL_ID` | Select one model from a multi-model directory |
| `--timeout 10m` | How long the CLI observes the run, not how long the runtime records |

Ctrl-C requests cancellation and retains available output. After a lost terminal or observation timeout, capture still ends at its original deadline. Use the printed inspection command to check progress.

## Common commands

Select one model from a multi-model deployment:

```bash
foretoken profile examples/multi-model-quickstart \
  --model Qwen/Qwen3-0.6B \
  --profile-engine pytorch --profile-duration 15s
```

Allow more time to observe a slow export without extending capture:

```bash
foretoken profile examples/quickstart \
  --profile-engine pytorch --profile-duration 15s --timeout 20m
```

## Inspect results

Each runtime stores one manifest and its native `.pt.trace.json` files below:

```text
profiles/runs/<run-uid>/<runtime-id>/
```

Access the files through the storage system backing the RuntimeCache PVC, then inspect traces with Perfetto or another compatible viewer. Cancellation may retain incomplete output, and later captures use separate run directories.

Profiling adds CPU/GPU overhead and can produce large files even in a short window. Use a small diagnostic deployment and a short duration. A native profiler failure may terminate that runtime, so use a service where interruption is acceptable.
