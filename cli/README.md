<!--
SPDX-License-Identifier: Apache-2.0
SPDX-FileCopyrightText: Copyright contributors to the Foretoken project
-->

# Foretoken CLI

English | [简体中文](README_zh.md)

Use `foretoken` to manage inference deployments, check services, run benchmarks, and capture execution profiles. For a first deployment, follow the [Quick Start](../README.md#quick-start); for agent-driven inference optimization, [start an RSI task](../rsi-playbooks/README.md#start-an-optimization-task).

| Task | Commands |
| --- | --- |
| [Environment and platform](#environment-and-platform) | `cluster create`, `cluster delete`, `install`, `uninstall` |
| [Service deployment](#service-deployment) | `deploy`, `stop`, `delete`, `endpoint` |
| [Status and logs](#status-and-logs) | `status` |
| [Benchmarks and results](#benchmarks-and-results) | `perf`, `eval`, `plot` |
| [Profiling](#profiling) | `deploy --profile`, `perf --profile`, `profile view` |

`foretoken --help` lists all commands. Use a command's `--help`, such as `foretoken status --help`, for its full options.

## Before you start

### Install the command-line tool

With Python 3.11 or later:

```bash
pip install foretoken
foretoken --version
```

The deployment and local-cluster examples use repository files. Get them and run those examples from the repository root:

```bash
git clone https://github.com/shiweijiezero/foretoken.git
cd foretoken
```

For CLI development, use `pip install -e .` in this directory. For uv, create and activate a virtual environment with `uv venv` and `source .venv/bin/activate`, then use `uv pip install foretoken`.

Platform installation and service deployment need `kubectl`, Helm 4.2.4 or later, and a kubeconfig for the target cluster. Benchmarking an existing URL and plotting local results do not require Kubernetes.

## Environment and platform

### cluster create / cluster delete

Create a local development cluster, or remove it when no longer needed. Skip creation when using an existing Kubernetes cluster.

```bash
# GPU cluster on a Linux host; select physical GPU 0
foretoken cluster create k3d --name foretoken-dev --gpus 0

# CPU-oriented development cluster
foretoken cluster create kind --name foretoken-dev
```

Creation selects the new Kubernetes context. k3d requires Docker, the NVIDIA driver and Container Toolkit, and k3d; kind requires Docker and kind. Both accept `--config` for a custom cluster configuration. Host setup is covered by the [k3d](../docs/deployment/k3d.md) and [kind](../docs/deployment/kind.md) guides.

To remove the whole cluster and its workloads:

```bash
foretoken cluster delete k3d --name foretoken-dev
```

Use `kind` instead of `k3d` for a kind cluster. The repository's `data/` directory is retained.

<a id="install-the-kubernetes-platform"></a>
<a id="current-source"></a>

### install

Install or update the shared Foretoken platform. This prepares the cluster for model deployments, including monitoring and persistent logs; deploy a model with `foretoken deploy`.

```bash
# Install the published platform
foretoken install

# Build from the current Foretoken checkout
foretoken install -e .
```

The cluster needs a default StorageClass, and GPU nodes need their vendor driver and device plugin. New installations use direct `LoadBalancer` access. Updated platform defaults take effect when services are redeployed.

Use `--frontend-mode gateway` for [Gateway access](../README.md#gateway-mode). To reuse a Gateway, also provide `--gateway-name` and `--gateway-namespace`; select a listener with `--gateway-section-name` when needed.

`--values` applies [Helm settings](../deploy/charts/foretoken/values.yaml), and `--oci-registry` selects the registry prefix for default images and Charts. See [Observability](../observability/README.md#platform-settings) for monitoring settings and [model storage](../docs/features/model-storage.md#distribute-models-across-nodes) for shared model distribution.

For source builds, standard kind and k3d contexts load images directly; other clusters require `--registry`. See [source deployment](../docs/deployment/source.md) for registry setup and deploying code changes.

### uninstall

Remove the shared platform after deleting its model deployments. The Kubernetes cluster, Foretoken CRDs, stored logs, and reused cluster components are retained.

```bash
foretoken uninstall
```

## Service deployment

These commands take a Kustomize directory containing `kustomization.yaml` and its referenced resources. The examples use the [single-model Quick Start](../examples/quickstart/qwen3-0.6b/README.md); its resource requirements and configuration are documented there.

### deploy

Create, update, or resume the directory's model and frontend services, and wait for them to reach their requested state.

```bash
foretoken deploy examples/quickstart/qwen3-0.6b --timeout 20m
```

Rerun the command after configuration changes or, with a source installation, code changes. `--timeout` sets the CLI wait, not the model's startup limit. If the wait fails, services remain in the cluster; use `status` to inspect them.

### stop

Stop serving and release GPUs while retaining configuration, storage, and build caches. Run `deploy` on the same directory to resume.

```bash
foretoken stop examples/quickstart/qwen3-0.6b
```

### delete

Delete the resources selected by the deployment directory and clean up unused build resources. If the configuration includes a Namespace, deletion also removes that namespace and all its resources.

```bash
foretoken delete examples/quickstart/qwen3-0.6b
```

Cache retention follows the [storage configuration](../docs/features/model-storage.md).

### endpoint

Print the public frontend URL for an API client. Use `--host` to get the request's `Host` header value, including the routing hostname for an HTTP Gateway.

```bash
# Public URL
foretoken endpoint examples/quickstart/qwen3-0.6b

# HTTP Host value
foretoken endpoint examples/quickstart/qwen3-0.6b --host
```

The command waits for an address; use `deploy` to wait for service readiness. See [sending a request](../README.md#4-send-a-test-request) for a client example.

## Status and logs

### status

Inspect service readiness, startup progress, and Pod/container failure messages. Select a deployment directory or a namespace:

```bash
# Services in one deployment
foretoken status examples/quickstart/qwen3-0.6b

# All model and frontend services in a namespace
foretoken status -n foretoken-demo

# Follow state changes and container logs
foretoken status -n foretoken-demo --watch
```

`--watch` also works with a deployment directory. Ctrl+C ends the watch and leaves services running. For persistent logs, metrics, and alerts, see [Observability](../observability/README.md).

## Benchmarks and results

`perf` and `eval` accept a deployment directory or an existing API endpoint. For a multi-model deployment, add `--model` with the public model identifier.

Benchmarks reuse existing services; run `deploy` first to measure configuration or source changes. If the deployment does not exist, they temporarily start services using the installed platform and clean up their serving workloads afterward.

### perf

Measure response latency, request throughput, and token throughput under a workload.

```bash
# Benchmark a Foretoken deployment
foretoken perf examples/quickstart/qwen3-0.6b --num-prompts 20 --output local

# Benchmark an existing API; replace the URL and model with your service's values
foretoken perf --url http://localhost:8000/v1/chat/completions \
  --model Qwen/Qwen3-0.6B --prompt Hello --num-prompts 20 --output local
```

URL mode takes the full request endpoint and an explicit prompt or dataset. `--output local` saves each run under `results/` without requiring a W&B account. See [performance benchmarks](../benchmarks/docs/perf/README.md) for datasets, concurrency, arrival rate, and parameter sweeps.

### eval

Score model answers using lm-evaluation-harness or EvalScope. This example evaluates 100 GSM8K math problems:

```bash
foretoken eval examples/quickstart/qwen3-0.6b \
  --evaluator lm-eval --tasks gsm8k --limit 100 --output local
```

Task options follow the selected framework. See [quality evaluation](../benchmarks/docs/eval/README.md) for other tasks and EvalScope, or [distribution comparison](../benchmarks/docs/eval/distribution-comparison.md) for candidate/reference comparisons with `--reference`.

### plot

Generate figures from saved benchmark results without rerunning inference. To plot the results saved by the examples above:

```bash
foretoken plot results
```

Figures are written to `results/plots/`. Pass individual run directories to select or compare particular runs. See [plotting options](../benchmarks/docs/recipes.md#plot-saved-results) and [W&B result uploads](../benchmarks/docs/perf/wandb.md#resume-an-upload).

## Profiling

### deploy --profile / perf --profile

Record a model's CPU/GPU execution timeline. `deploy --profile` captures external traffic after deployment is ready; `perf --profile` generates its own benchmark workload.

```bash
# Capture requests sent by an application or another terminal
foretoken deploy examples/quickstart/qwen3-0.6b \
  --profile --profile-engine pytorch --profile-duration 15s --timeout 20m

# Generate traffic and capture its execution
foretoken perf examples/quickstart/qwen3-0.6b \
  --profile --profile-engine pytorch --profile-duration 15s \
  --num-prompts 2 --max-tokens 128 --output local
```

Capture requires a Foretoken deployment directory, `--profile-engine`, and `--profile-duration`. Supported engines are `pytorch`, NVIDIA `nsight`, and MetaX `mctracer`. The Quick Start provides persistent capture storage; see the [profiling guide](../benchmarks/docs/profile/README.md) for tool setup.

### profile view

Browse captures retained in the cluster. Run on the browser's computer with a kubeconfig for that cluster:

```bash
foretoken profile view
```

Open the printed local URL. Add `-n foretoken-demo` to select one namespace. Ctrl+C closes the viewer, leaving model services and captures in place.
