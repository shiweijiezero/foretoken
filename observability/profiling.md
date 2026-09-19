<!--
SPDX-License-Identifier: Apache-2.0
SPDX-FileCopyrightText: Copyright contributors to the Foretoken project
-->

# Profiling

English | [简体中文](profiling_zh.md)

Inspect vLLM execution with PyTorch Profiler, NVIDIA Nsight Systems, or MetaX mcTracer. Profiling supports NVIDIA and MetaX GPUs and requires a [source-installed](../docs/custom-deployment.md) CLI and platform. Results use persistent RuntimeCache storage, which the Quick Start already configures.

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

## MetaX mcTracer

The model-server image must provide the matching MACA SDK's `mcTracer` executable on `PATH` and `libmcpti.so`; update older images before capturing.

In your deployment directory, add this under `spec` in the ModelService YAML:

```yaml
profiling:
  engine: mctracer
```

Then use either deploy or benchmark command above with `--profile-engine mctracer`. YAML selects the profiler prepared by the model processes; the CLI flag selects the capture engine and must match it. Omitting the YAML field prepares PyTorch. After changing it, use deploy to update the model processes. Benchmark can create an absent deployment and reuses an existing service unchanged.

Foretoken prepares the workers at model startup and exports a native JSON report for each worker. Stopping capture leaves inference running, and subsequent captures reuse the same model processes. CUDA Graph does not need to be disabled.

## Nsight Systems

Nsight Systems records CUDA and NVTX timelines. Select it before model startup with `ModelService.spec.profiling.engine: nsight`; changing the tool replaces the model processes. Omitting this field prepares PyTorch instead. The capture's `--profile-engine` must match the prepared tool.

### Prepare the diagnostic image

After source installation, build the Linux x86_64 diagnostic image from the local model-server build. Set `NSIGHT_IMAGE` to an image reference you can push and your cluster can pull:

```bash
docker build -f deploy/inference-engines/nsight/Dockerfile \
  --build-arg MODEL_SERVER_IMAGE=foretoken-dev-model-server \
  -t "$NSIGHT_IMAGE" deploy/inference-engines/nsight
docker push "$NSIGHT_IMAGE"
```

Save the following as `nsight-values.yaml`, replacing `YOUR_NSIGHT_IMAGE` with that image reference:

```yaml
runtime:
  vllm:
    nsightImage: YOUR_NSIGHT_IMAGE
```

Add `--values nsight-values.yaml` to the source installation command used for this cluster. Only models selecting Nsight use the diagnostic image.

### Capture

The [Nsight example](../examples/profile/nsight/README.md) selects the tool and uses the Quick Start's persistent storage:

```bash
pip install -e '.[bench]'
foretoken bench examples/profile/nsight \
  --profile --profile-engine nsight --profile-duration 15s \
  --number 2 --max-tokens 128 --output local
```

For external traffic, use `foretoken deploy examples/profile/nsight --profile --profile-engine nsight --profile-duration 15s --timeout 20m` instead. This leaves the service running after capture; repeat the command to capture another window.

## Inspect results

Run on your local computer with a kubeconfig for the target cluster:

```bash
foretoken profile view
```

Open the printed URL to browse capture directories and their subfolders. PyTorch traces open in Perfetto; the browser needs access to `ui.perfetto.dev`. For Nsight, download the `.nsys-rep` report and open it in Nsight Systems, or download the SQLite export for analysis. For mcTracer, download the native JSON and open it in [mcTracer-Viewer](https://developer.metax-tech.com/api/client/document/preview/1190/index.html). Press Ctrl+C to close the viewer; files are preserved.

When the deployment and capture records are no longer needed, clean up with:

```bash
foretoken delete examples/quickstart
```

Use `examples/profile/nsight` instead when cleaning up the Nsight example. Profiling adds overhead. Use a separate run without `--profile` for latency and throughput comparisons.
