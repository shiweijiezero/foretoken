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

## Capture a benchmark workload

To generate requests and capture them in the same command, install the benchmark dependencies from source (`pip install -e '.[bench]'`) and use an already deployed diagnostic service:

```bash
foretoken bench examples/quickstart \
  --profile --profile-engine pytorch --profile-duration 15s \
  --number 2 --max-tokens 128 --output local
```

The command prepares the workload, waits until all selected runtimes report `Capturing`, then releases requests through the normal frontend. It fails without sending requests if capture ends before that readiness is observed. When requests finish, it requests `Finish` and waits for export. A window that ends first does not truncate the benchmark. Use one generated workload with `--rate -1`; URL-only services, trace replay, sweeps and multiple datasets are not supported in this mode.

For an existing NodePort, proxy, or port-forwarded frontend, keep the Kustomize path and add `--url` with its full Chat Completions URL. The URL must serve that same deployment; the path still selects the capture target. See [explicit benchmark endpoints](../benchmarks/README.md#an-explicit-endpoint-for-a-deployment).

`--wait-timeout` bounds each capture startup/completion wait. Ctrl-C or a workload failure requests cancellation; an unsuccessful capture makes the command fail. Existing services and RuntimeCache output are retained. No port forwarding or profiling-specific service YAML is needed.

With local output, `profile.json` records the ProfileRun identity, last observed status, capture-readiness observation and request timestamps. These are client observations, not exact GPU event boundaries: inspect the native trace and manifest to determine what was recorded. Use a separate benchmark without profiling for latency and throughput comparisons.

## Inspect results

Each runtime stores one manifest and its native `.pt.trace.json` files below:

```text
profiles/runs/<run-uid>/<runtime-id>/
```

The Quick Start examples store results under the repository-root `data/profiles/runs/`. For other deployments, use the PVC and relative path printed by the command.

Profiling adds CPU/GPU overhead and can produce large files even in a short window. Use a small diagnostic deployment and a short duration. A native profiler failure may terminate that runtime, so use a service where interruption is acceptable.
