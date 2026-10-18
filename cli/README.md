<!--
SPDX-License-Identifier: Apache-2.0
SPDX-FileCopyrightText: Copyright contributors to the Foretoken project
-->

# Foretoken command-line tool

English | [简体中文](README_zh.md)

`foretoken` prepares Kubernetes environments, operates model services, measures performance and quality, and captures execution profiles. For a first deployment from installation to a successful request, follow the [Quick Start](../README.md#quick-start).

| Category | Command | When to use it |
| --- | --- | --- |
| [1. Environment and platform](#1-environment-and-platform) | `foretoken cluster create` | Create a local k3d GPU cluster or kind development cluster. |
| | `foretoken install` | Install or update shared Foretoken APIs, dependencies, and deployment defaults. |
| | `foretoken uninstall` | Remove the platform after deleting its model deployments. |
| | `foretoken cluster delete` | Remove a local cluster and all workloads in it. |
| [2. Service deployment](#2-service-deployment) | `foretoken deploy` | Create, update, or resume the services defined by a deployment directory. |
| | `foretoken endpoint` | Get a deployment's public URL or HTTP routing hostname. |
| | `foretoken stop` | Release serving resources while keeping configuration, storage, and build caches. |
| | `foretoken delete` | Remove a deployment and its associated resources. |
| [3. Status and logs](#3-status-and-logs) | `foretoken status` | Inspect readiness and failures, or follow status and container logs with `--watch`. |
| [4. Benchmarks and results](#4-benchmarks-and-results) | `foretoken perf` | Measure latency and throughput under a chosen workload. |
| | `foretoken eval` | Score model answers or compare a candidate with a reference model. |
| | `foretoken plot` | Plot or compare saved benchmark results, or upload them to W&B. |
| [5. Profiling](#5-profiling) | `foretoken deploy --profile` / `foretoken perf --profile` | Record CPU/GPU execution while handling external traffic or a benchmark workload. |
| | `foretoken profile view` | Browse current and retained captures in a local web viewer. |

## Before you start

### Install the command-line tool

Use Python 3.11 or later:

```bash
pip install foretoken
foretoken --version
```

With uv, create and activate a virtual environment using `uv venv` and `source .venv/bin/activate`, then run `uv pip install foretoken`.

Use `foretoken --help` to list commands. Add `--help` to a command for its arguments, for example `foretoken deploy --help` or `foretoken cluster create --help`.

### Prepare the examples and cluster access

The local-cluster commands and deployment examples below use files from the repository. Get them and run those commands from the repository root:

```bash
git clone https://github.com/shiweijiezero/foretoken.git
cd foretoken
```

To develop the CLI itself, install it from this directory with `pip install -e .`.

Kubernetes operations need `kubectl`, Helm 4.2.4 or later, and a kubeconfig selecting the target cluster. An existing cluster can be used directly; local cluster creation is optional. GPU nodes need their vendor driver and Kubernetes device plugin. Benchmarking an existing URL and plotting local results do not require Kubernetes.

## 1. Environment and platform

### `cluster create`: prepare a local environment

On a Linux GPU host with Docker, NVIDIA Container Toolkit, and k3d installed:

```bash
foretoken cluster create k3d --name foretoken-dev --gpus 0
```

`--gpus` selects physical GPU indices from `nvidia-smi`; use `0,1` for two GPUs. The command prepares the NVIDIA device plugin and selects the new Kubernetes context. Host setup is covered by the [k3d guide](../docs/deployment/k3d.md).

For CPU-oriented platform development, install Docker and kind, then use:

```bash
foretoken cluster create kind --name foretoken-dev
```

The default kind configuration does not expose GPUs. See the [kind guide](../docs/deployment/kind.md) for its supported workflow. Both cluster types accept `--config` with a custom cluster configuration file.

### Install the Kubernetes platform

`foretoken install` prepares the cluster for model deployments:

```bash
foretoken install
```

It installs or updates the shared Foretoken APIs and dependencies, enables monitoring and persistent logs, and selects the NVIDIA or MetaX runtime. The cluster needs a default StorageClass for persistent storage. The default frontend access mode uses a `LoadBalancer` Service.

Run the same command to update the platform. Updated defaults take effect when services are redeployed; installation does not deploy a model. Use `foretoken deploy` for that step.

### Current source

To build the platform from the current checkout:

```bash
foretoken install -e .
```

Subsequent `foretoken deploy` commands build controller and serving-code changes from the current Foretoken directory. Standard kind and k3d contexts load built images directly; other clusters need `--registry` with a registry reachable by build workloads and nodes. See [source deployment](../docs/deployment/source.md) for remote registries, engine-source changes, and parallel candidates.

### Customize installation

| Need | Option or guide |
| --- | --- |
| Route through a hostname and Gateway | `--frontend-mode gateway`; see [Gateway setup and reuse](../docs/deployment/platform.md#gateway-access). |
| Choose images, GPU resources, or other platform settings | `--values`; see [platform configuration](../docs/deployment/platform.md#custom-platform-values). |
| Select an image and Chart source | `--oci-registry`; see [registry selection](../docs/deployment/platform.md#image-and-chart-sources). |
| Configure monitoring, Grafana login, or log retention | [Observability](../observability/README.md#platform-settings). |
| Assign service IPs or share model downloads | [LoadBalancer addresses](../docs/deployment/platform.md#loadbalancer-addresses) and [model distribution](../docs/deployment/platform.md#share-model-downloads-with-dragonfly). |

### `uninstall`: remove the platform

After removing all model deployments with `foretoken delete`:

```bash
foretoken uninstall
```

Uninstall removes the platform and its managed dependencies, preserving CRDs, log storage, and reused cluster components. Managed LeaderWorkerSet, MetalLB, and Dragonfly installations are retained while other workloads use them. The Kubernetes cluster remains available.

### `cluster delete`: remove a local environment

When the local cluster is no longer needed:

```bash
foretoken cluster delete k3d --name foretoken-dev
```

Use `kind` instead of `k3d` for a kind cluster. This removes the cluster and its workloads; the repository's `data/` directory is retained.

## 2. Service deployment

Service commands take a Kustomize directory: a directory containing `kustomization.yaml` and the Kubernetes resources it selects. A deployment contains one `FrontendService` and one or more `ModelService` resources in the same namespace.

The examples use the [single-model Quick Start](../examples/quickstart/qwen3-0.6b/README.md), which requests one GPU, 8 CPU, and 52 GiB memory in total. The platform needs additional capacity. For multiple models behind one frontend, use the [multi-model example](../examples/quickstart/multi-model/README.md).

### `deploy`: create, update, or resume services

With the platform installed:

```bash
foretoken deploy examples/quickstart/qwen3-0.6b --timeout 20m
```

The command applies the configuration and shows service status and container logs until services reach their requested running or stopped state. Rerun it after editing the configuration or to resume a stopped deployment. Unchanged deployments reuse running applications.

`--timeout` controls CLI waits and defaults to ten minutes; source compilation runs separately. Each model's `spec.timeouts.startup` controls model preparation and startup. A readiness failure or timeout leaves the deployment in Kubernetes: use `status` to investigate, then fix configuration errors and redeploy.

### `endpoint`: get the public address

```bash
foretoken endpoint examples/quickstart/qwen3-0.6b
```

The output is the frontend URL for clients and [test requests](../README.md#4-send-a-test-request). The command waits for the LoadBalancer or Gateway address; `deploy` waits for service readiness.

For an HTTP Gateway, get the hostname to send in the request's `Host` header:

```bash
foretoken endpoint examples/quickstart/qwen3-0.6b --host
```

For direct access, `--host` returns the host and any port in the URL.

### `stop`: release resources between runs

```bash
foretoken stop examples/quickstart/qwen3-0.6b
```

The command waits for frontend and model execution to stop, releasing GPUs while retaining configuration, storage, and build caches. Resume with `foretoken deploy` on the same directory; the model processes restart and load weights again.

### `delete`: remove a deployment

```bash
foretoken delete examples/quickstart/qwen3-0.6b
```

This deletes the resources selected by the directory and cleans up unused build resources. If the configuration includes a Namespace, deletion also removes that namespace and all its resources. Cache retention follows the [storage configuration](../docs/features/model-storage.md).

## 3. Status and logs

Use `foretoken status` to check service readiness, investigate a failed deployment, or follow startup progress. It reads the current state without applying configuration.

### Inspect one deployment

```bash
foretoken status examples/quickstart/qwen3-0.6b
```

The output shows each service's state and diagnostic details, including Kubernetes waiting and termination reasons and the previous container attempt after a restart.

### Inspect a namespace

```bash
foretoken status -n foretoken-demo
```

This selects every Foretoken model and frontend service in the namespace, including those from other deployment directories.

### Follow status and container logs

```bash
foretoken status -n foretoken-demo --watch
```

`--watch` follows state changes and Pod/container logs until Ctrl+C. It also works with a deployment directory. Ending the watch leaves services running. For persistent logs, metrics, and alerts, see [Observability](../observability/README.md).

## 4. Benchmarks and results

Use `perf` for response speed and capacity, `eval` for model quality, and `plot` for saved results. Both benchmark commands accept a Kustomize directory, or `--url` and `--model` for an existing OpenAI-compatible service. For a multi-model deployment, select the public model identifier with `--model`.

Benchmarks reuse deployed services, so run `deploy` first to measure configuration or source changes. If the deployment does not exist, the benchmark temporarily starts services using the installed platform and cleans up its serving workloads when finished.

### `perf`: measure latency and throughput

```bash
foretoken perf examples/quickstart/qwen3-0.6b --num-prompts 20 --output local
```

The summary reports request success, response latency, and throughput; `--output local` saves the results for later analysis. See [performance examples](../benchmarks/docs/perf/README.md) for prompts, datasets, concurrency, arrival rate, and parameter sweeps.

For an existing service, supply its full API endpoint, model identifier, and a prompt or dataset. Replace the URL and model below with your service's values:

```bash
foretoken perf --url http://localhost:8000/v1/chat/completions \
  --model Qwen/Qwen3-0.6B --prompt Hello --num-prompts 20 --output local
```

### `eval`: score answers or compare models

```bash
foretoken eval examples/quickstart/qwen3-0.6b \
  --evaluator lm-eval --tasks gsm8k --limit 100 --output local
```

This scores 100 GSM8K math problems using lm-evaluation-harness. EvalScope is also supported; tasks and scoring options use the selected framework's syntax. See [quality evaluation](../benchmarks/docs/eval/README.md) for datasets and outputs, or [distribution comparison](../benchmarks/docs/eval/distribution-comparison.md) to compare a candidate's probabilities with a reference using `--reference`.

### `plot`: analyze saved results

The examples above save each run in a separate directory under `results/`. Plot those saved runs together:

```bash
foretoken plot results
```

Figures are saved to `results/plots/`. To select specific runs, pass one or more result directories printed by `perf` or `eval`. During a benchmark, `--output plot` exports figures directly.

For W&B, run `wandb login` and select `--output local,wandb` when starting the benchmark. Runs recorded with W&B output, including offline runs, can later be uploaded with `foretoken plot results --sync_wandb`. See [plotting examples](../benchmarks/docs/recipes.md#plot-saved-results) and [W&B uploads](../benchmarks/docs/perf/wandb.md#resume-an-upload).

## 5. Profiling

Profiling records the model's CPU/GPU execution timeline to locate bottlenecks. Choose `pytorch`, NVIDIA `nsight`, or MetaX `mctracer` with `--profile-engine`, and set the capture limit with `--profile-duration`. The deployment needs persistent capture storage; the Quick Start already provides it. Tool-specific preparation is covered by the [profiling guide](../benchmarks/docs/profile/README.md).

### Capture a benchmark workload

```bash
foretoken perf examples/quickstart/qwen3-0.6b \
  --profile --profile-engine pytorch --profile-duration 15s \
  --num-prompts 2 --max-tokens 128 --output local
```

`perf` sends the workload and coordinates the capture, so no separate traffic generator is needed.

### Capture external traffic

To record requests from an application or another traffic generator:

```bash
foretoken deploy examples/quickstart/qwen3-0.6b \
  --profile --profile-engine pytorch --profile-duration 15s --timeout 20m
```

This deploys the service and records a bounded window. Send traffic while the capture is active. For a directory containing several models, add `--model` to select the model to profile.

### `profile view`: browse captures

On the computer where you use your browser, with a kubeconfig for the cluster:

```bash
foretoken profile view
```

Open the printed local URL to browse current and historical captures. Use `-n foretoken-demo` to limit the viewer to the example namespace. Ctrl+C closes the viewer; the model service remains running.
