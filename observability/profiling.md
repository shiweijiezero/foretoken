<!--
SPDX-License-Identifier: Apache-2.0
SPDX-FileCopyrightText: Copyright contributors to the Foretoken project
-->

# Profiling

English | [简体中文](profiling_zh.md)

Use PyTorch Profiler to inspect CPU/GPU execution during inference. Profiling supports vLLM on NVIDIA and [MetaX GPUs](../docs/metax-deployment.md) and requires a [source-installed](../docs/custom-deployment.md) CLI and platform. Results use persistent RuntimeCache storage, which the Quick Start already configures.

## Capture a benchmark workload

Run from the repository root:

```bash
pip install -e '.[bench]'
foretoken bench examples/quickstart \
  --profile --profile-engine pytorch --profile-duration 15s \
  --number 2 --max-tokens 128 --output local
```

This mode supports a single generated workload from a Kustomize deployment with the default `--rate -1`.

## Deploy and capture external traffic

```bash
foretoken deploy examples/quickstart \
  --profile --profile-engine pytorch --profile-duration 15s
```

Capture starts when the service is ready and records externally supplied requests. The service remains running afterwards. Use `--model MODEL_ID` to select the capture target in a multi-model deployment. `--profile-duration` sets the maximum recording time; benchmark capture also stops when the workload finishes early.

## Inspect results

Run on your local computer with a kubeconfig for the target cluster:

```bash
foretoken profile view
```

Open the printed URL to browse capture directories and their subfolders. Click a trace to view it in Perfetto. The browser needs access to `ui.perfetto.dev`. Press Ctrl+C to close the viewer; files are preserved.

When the deployment and capture records are no longer needed, clean up with:

```bash
foretoken delete examples/quickstart
```

Profiling adds overhead. Use a separate run without `--profile` for latency and throughput comparisons.
