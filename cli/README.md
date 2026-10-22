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

Service deployment needs `kubectl` and a kubeconfig for the target cluster. Platform installation and source development also need Helm 4.2.4 or later. Each platform or service command keeps its initial Kubernetes context. Benchmarking an existing URL and plotting local results do not require Kubernetes.

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

An administrator installs the shared Foretoken platform once for the cluster. It prepares APIs, dependencies, monitoring, persistent logs and deployment defaults in the `foretoken-platform` namespace. Users then create independent instances with `foretoken deploy`.

```bash
# Install the published platform
foretoken install

# Build from the current Foretoken checkout
foretoken install -e .
```

The cluster needs a default StorageClass, and GPU nodes need their vendor driver and device plugin. New installations share one Gateway entry point. Use `--frontend-mode local` for a separate `LoadBalancer` Service per instance. Updating the platform does not restart existing instances; redeploy an instance to adopt the updated defaults.

To reuse an existing Gateway, specify `--frontend-mode gateway`, `--gateway-name` and `--gateway-namespace`; select a listener with `--gateway-section-name` when needed. Custom domains and HTTPS are covered by [Gateway configuration](../README.md#gateway-mode).

`--values` applies [Helm settings](../deploy/charts/foretoken/values.yaml), and `--oci-registry` selects the registry prefix for default images and Charts. See [Observability](../observability/README.md#platform-settings) for monitoring settings and [model storage](../docs/features/model-storage.md#distribute-models-across-nodes) for shared model distribution.

New managed Gateways listen for HTTP on port 8081. To use another listener port, add the following to `deploy/platform-values.yaml` and pass `--values deploy/platform-values.yaml` with your installation command:

```yaml
frontend:
  gateway:
    port: 8082
```

For source builds, standard kind and k3d contexts load images directly; other clusters require `--registry`. Explicit `deploy -e PATH` updates only the selected instance's controller and serving applications. A new workstation can use the source-installed platform without reinstalling it. See [source deployment](../docs/deployment/source.md) for setup, engine checkouts and parallel candidates.

### Prepare a namespace for members

After installing the platform, an administrator grants service access in an existing namespace. Replace `team-inference` with that namespace and `MEMBER_ID` with the Kubernetes identity in the member's kubeconfig:

```bash
kubectl create rolebinding foretoken-operator --namespace team-inference \
  --clusterrole foretoken-operator --user MEMBER_ID
```

This RoleBinding permits members to deploy, inspect, stop and delete instances. Source builds with `deploy -e` require platform-level access and are run by the administrator or an authorized, trusted developer.

Members select the assigned namespace in their kubeconfig once:

```bash
kubectl config set-context --current --namespace team-inference
```

Daily commands use that namespace automatically. Members do not create Namespace resources or install a controller. The platform prepares each instance's controller and authorized storage when it is deployed; see [shared directories](../docs/features/model-storage.md#share-model-files-across-namespaces).

### uninstall

The platform administrator removes the shared platform and its managed development storage after deployment instances are gone. The Kubernetes cluster, Foretoken CRDs, stored logs and reused cluster components are retained.

```bash
foretoken uninstall
```

<a id="deploy-and-operate-model-services"></a>

## Service deployment

Deploy one frontend and its models from a Kustomize directory. Instances use the current kubeconfig namespace. Each has its own controller and services, so updates, stop and deletion do not affect neighboring instances in the same namespace.

The examples use the [single-model Quick Start](../examples/quickstart/qwen3-0.6b/README.md); its resource requirements and configuration are documented there. Without `--name`, the instance name comes from `metadata.name` of the `FrontendService` in [`frontend.yaml`](../examples/quickstart/qwen3-0.6b/frontend.yaml): `qwen3-0-6b` in this example. The commands below override it with `--name qwen` and use `qwen` afterward.

### deploy

Create, update or resume an instance, and wait for its services to reach their requested state:

```bash
foretoken deploy examples/quickstart/qwen3-0.6b --name qwen --timeout 20m
```

Use another `--name` to run an independent instance from the same configuration. Ordinary deployment uses installed applications without building source. Add `-e PATH` for source development on a source-installed platform.

Rerun the command after configuration changes. `--timeout` limits the CLI wait, not the model's `spec.timeouts.startup`; source compilation runs separately. If preparation fails or readiness times out, services remain in the cluster. Use `status` to inspect them, then correct the configuration and redeploy when needed.

### stop

Stop the selected instance and release GPUs while retaining configuration, model-cache references and runtime state. Rerun its deployment command to resume.

```bash
foretoken stop qwen
```

<a id="clean-up"></a>

### delete

Delete only the selected instance. The command waits for its services and workloads to retire, then removes its controller and instance resources. It does not delete the Kubernetes namespace, neighboring instances or administrator-provided storage.

```bash
foretoken delete qwen
```

Development environments and incremental build state are retained for later source deployments. Managed model downloads are collected after their last service reference is released; `stop` retains those references. Dynamic volumes follow their retention and StorageClass reclaim policies. See [model storage](../docs/features/model-storage.md#stop-delete-and-reuse).

### endpoint

Print the frontend URL, or its request `Host` with `--host`. HTTP Gateway routing is configured automatically for each instance. For a custom domain, HTTPS or a hostname-restricted listener, see [Gateway configuration](../README.md#gateway-mode).

```bash
# Public URL
foretoken endpoint qwen

# HTTP Host value
foretoken endpoint qwen --host
```

The command waits for an address; use `deploy` to wait for service readiness. See [sending a request](../README.md#4-send-a-test-request) for a client example.

## Status and logs

### status

Inspect service readiness, startup progress and Pod/container failure messages. Select an instance, list instances in the current namespace, or follow the selected instance's state and logs:

```bash
foretoken status qwen
foretoken status
foretoken status qwen --watch
```

`status`, `stop`, `delete` and `endpoint` also accept a Kustomize path. Pass the same `--name` with that path when selecting a named instance, for example `foretoken status examples/quickstart/qwen3-0.6b --name qwen`.

Ctrl+C ends a watch and leaves services running. For persistent logs, metrics and alerts, see [Observability](../observability/README.md).

## Benchmarks and results

`perf` and `eval` accept a deployment directory or an existing API endpoint. Use the deployment's `--name` to select a named instance. For multiple models, add `--model` with the public model identifier.

Benchmarks reuse existing services unchanged; run `deploy` first to measure configuration or source changes. If the instance does not exist, they temporarily start services using installed applications and clean up their serving workloads afterward.

### perf

Measure response latency, request throughput and token throughput under a workload:

```bash
# Benchmark the named Foretoken instance
foretoken perf examples/quickstart/qwen3-0.6b --name qwen --num-prompts 20 --output local

# Benchmark an existing API; replace the URL and model with your service's values
foretoken perf --url http://localhost:8000/v1/chat/completions \
  --model Qwen/Qwen3-0.6B --prompt Hello --num-prompts 20 --output local
```

URL mode takes the full request endpoint and an explicit prompt or dataset. `--output local` saves each run under `results/` without requiring a W&B account. See [performance benchmarks](../benchmarks/docs/perf/README.md) for datasets, concurrency, arrival rate and parameter sweeps.

### eval

Score model answers using lm-evaluation-harness or EvalScope. This example evaluates 100 GSM8K math problems:

```bash
foretoken eval examples/quickstart/qwen3-0.6b --name qwen \
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

Record a model's CPU/GPU execution timeline. `deploy --profile` captures external traffic after deployment is ready; `perf --profile` generates its own workload.

```bash
# Capture requests sent by an application or another terminal
foretoken deploy examples/quickstart/qwen3-0.6b --name qwen \
  --profile --profile-engine pytorch --profile-duration 15s --timeout 20m

# Generate traffic and capture its execution
foretoken perf examples/quickstart/qwen3-0.6b --name qwen \
  --profile --profile-engine pytorch --profile-duration 15s \
  --num-prompts 2 --max-tokens 128 --output local
```

Capture requires a Foretoken deployment directory, `--profile-engine` and `--profile-duration`. Supported engines are `pytorch`, NVIDIA `nsight` and MetaX `mctracer`. The Quick Start provides persistent capture storage; see the [profiling guide](../benchmarks/docs/profile/README.md) for tool setup.

### profile view

Browse captures retained in the cluster. Run on the browser's computer with a kubeconfig for that cluster:

```bash
foretoken profile view
```

Open the printed local URL. Add `-n team-inference` to limit discovery to that namespace. Ctrl+C closes the viewer, leaving model services and captures in place.
