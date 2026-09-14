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

The command reads the directory to identify the deployed service without applying it. If it contains several models, select one with `--model MODEL_ID`. It does not generate traffic; send requests through the normal frontend while capture is running.

The selected ModelService must use persistent RuntimeCache storage. The maintained Quick Start already declares it in `cache.yaml`; other deployments can follow [Model storage](../docs/model-storage.md). Profiling writes to the `profiles/` directory on the same RuntimeCache PVC. A service without persistent RuntimeCache storage must be redeployed with one before capture.

The runtime starts the recording after profiler startup, stops after the requested duration, and then exports the files. Export may take longer than recording. Normal completion leaves the model serving. The command prints the ProfileRun name and, on completion, the RuntimeCache PVC and path containing the results.

| Option | Meaning |
|---|---|
| `--profile-engine pytorch` | Required profiler selection; only PyTorch is available |
| `--profile-duration 15s` | Required recording duration; excludes startup and export |
| `--model MODEL_ID` | Select one model from a multi-model directory |
| `--timeout 10m` | How long the CLI observes the run, not how long the runtime records |

Ctrl-C requests cancellation and retains available output. After a lost terminal or observation timeout, capture still ends at its original deadline. Use the printed inspection command to check progress.

## Inspect results

Each runtime stores one manifest and its native `.pt.trace.json` files below:

```text
profiles/runs/<run-uid>/<runtime-id>/
```

For the default directory-backed Quick Start, `./data` is relative to the Kustomize directory, so an accessible checkout contains the results under `examples/quickstart/data/profiles/runs/`. For other deployments, use the PVC and relative path printed by the command; the data directory may be on the cluster rather than on the workstation.

Profiling adds CPU/GPU overhead and can produce large files even in a short window. Use a small diagnostic deployment and a short duration. A native profiler failure may terminate that runtime, so use a service where interruption is acceptable.
