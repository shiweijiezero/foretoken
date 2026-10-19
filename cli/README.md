<!--
SPDX-License-Identifier: Apache-2.0
SPDX-FileCopyrightText: Copyright contributors to the Foretoken project
-->

# Foretoken command-line tool

English | [简体中文](README_zh.md)

The Foretoken command-line tool installs the shared Kubernetes platform, deploys model services from Kustomize configurations, reports serving readiness, resolves frontend URLs, and runs benchmarks through one `foretoken` entry point.

## Before you start

Use Python 3.11 or later and `kubectl` with access to the target cluster. Platform installation and source development also need Helm 4.2.4 or later. Create a local cluster as shown below, or select an existing Kubernetes context. GPU nodes need their vendor driver and Kubernetes device plugin.

## Install the command-line tool

Install the published command-line tool with pip:

```bash
pip install foretoken

# From a source checkout:
# pip install -e .
```

Or create and activate a virtual environment with uv:

```bash
uv venv
source .venv/bin/activate
uv pip install foretoken
```

Run `foretoken --version` to check the installed CLI version.

## Create a local cluster

For a local GPU cluster on a Linux host with Docker, NVIDIA Container Toolkit, and k3d installed:

```bash
# Name the local cluster and use GPU index 0 from nvidia-smi.
# To use two GPUs, pass --gpus 0,1.
foretoken cluster create k3d --name foretoken-dev --gpus 0
```

For a local kind development cluster, install Docker, kind, kubectl, and Helm:

```bash
foretoken cluster create kind --name foretoken-dev
```

Remove a cluster created by the CLI with:

```bash
foretoken cluster delete k3d --name foretoken-dev
foretoken cluster delete kind --name foretoken-dev
```

## Install the Kubernetes platform

An administrator runs `foretoken install` once for the cluster to prepare shared APIs, dependencies, monitoring and deployment defaults. Platform resources use the `foretoken-platform` namespace. Gateway mode also prepares the shared entry point.

Users then deploy independent instances with `foretoken deploy`. Updating the platform does not restart existing instances; redeploy an instance to adopt the updated defaults.

### Default installation

The default installs the published platform and selects direct access through a `LoadBalancer` Service for model deployments:

```bash
foretoken install
```

Installation selects the NVIDIA or MetaX runtime and automatically reuses or installs LeaderWorkerSet and the shared RDMA device plugin. Explicit runtime settings in `--values` take precedence; in a mixed-GPU cluster, select a resource with `runtime.vllm.gpu.resourceName` or restrict the nodes with `runtime.vllm.gpu.nodeSelector`.

Log collection and persistence are enabled by default. See [Observability](../observability/README.md) for configuration, log queries, dashboards, and alerts.

### Gateway mode

Gateway mode creates a dedicated `GatewayClass` and `Gateway`, installing Envoy Gateway if no compatible controller is available:

```bash
foretoken install --frontend-mode gateway
```

With another Gateway Controller, reuse a Gateway managed by that controller:

```bash
foretoken install \
  --frontend-mode gateway \
  --gateway-name inference-gateway \
  --gateway-namespace gateway-system
```

Add `--gateway-section-name LISTENER` only when more than one listener matches.

### Current source

The cluster needs a default StorageClass for application files. Run from the Foretoken repository root:

```bash
foretoken install -e .
```

Add `-e .` to deployment commands to build controller, frontend and model-server changes from your current Foretoken directory. See the examples for [running different code versions in parallel](../docs/deployment/source.md#run-candidates-in-parallel) or [deploying local vLLM and MetaX plugin code](../docs/deployment/source.md#edit-an-inference-engine).

A standard active kind or k3d context loads the built images directly into its nodes. Other Kubernetes contexts need a registry reachable by the Build Pods and nodes. For an internal registry without authentication:

```bash
foretoken install -e . --registry registry.example.com:5000/foretoken
```

If the registry requires authentication, follow the [source deployment guide](../docs/deployment/kubernetes.md) before installation.

### Model distribution

To share public model downloads between nodes through Dragonfly, save this in `deploy/platform-values.yaml`:

```yaml
modelDistribution:
  dragonfly:
    enabled: true
```

For a published platform installation, apply the values with:

```bash
foretoken install --values deploy/platform-values.yaml
```

For a source installation, run `foretoken install -e . --values deploy/platform-values.yaml` from the repository root, retaining the original registry and engine-source options.

Installation prepares Dragonfly or reuses an existing installation. Models that require authentication and custom model endpoints download directly from their provider. To select a particular Dragonfly Helm release, set `existingRelease: {name: dragonfly, namespace: dragonfly-system}` under `modelDistribution.dragonfly`.

On NVIDIA clusters with RDMA, ModelExpress can load weights from running replicas. Add this alongside `dragonfly` to enable it:

```yaml
modelDistribution:
  modelexpress:
    enabled: true
```

Automatic weight transfer uses remote models with a persistent cache, data parallelism of one, and fixed expert placement. An explicit `load-format` remains unchanged. Each GPU worker selects a nearby available RDMA interface; replicas without a compatible source load the prepared files.

Reapply the installation command after changing either setting, then redeploy the affected services. Set `enabled: false` to disable it. `foretoken uninstall` removes managed Dragonfly resources once no workloads use them; reused installations are retained.

### Installation options

Use `--values` to customize platform settings. Without an override, each installation selects public sources for default platform images and Chart downloads. Chart downloads and image pulls during source builds can fall back to the original source when an automatically selected mirror fails. Use `--oci-registry` to select a registry explicitly; image references supplied through values remain unchanged. Source selection runs on the CLI host, so the selected registry must also be reachable from the cluster nodes.

Model services are reached through an IP address outside the cluster. k3d, k3s, and cloud clusters assign one automatically. Clusters built with kubeadm, RKE2, or kubespray have no address assignment by default, so installation there ends with `LoadBalancer support Not verified`. Give Foretoken a range of unused addresses in the nodes' subnet, confirmed with the cluster administrator, and it assigns them to services:

```yaml
loadBalancer:
  managedAddresses:
    - 192.168.1.240-192.168.1.250
```

## Prepare a namespace for members

After installing the platform, an administrator grants service access in an existing namespace. Replace `team-inference` with that namespace and `MEMBER_ID` with the Kubernetes identity in the member's kubeconfig:

```bash
kubectl create rolebinding foretoken-operator --namespace team-inference \
  --clusterrole foretoken-operator --user MEMBER_ID
```

Platform installation does not grant permissions to users. The RoleBinding lets members deploy, inspect, stop and delete instances without managing the shared platform. Source development with `deploy -e` requires separate build permissions.

Members select the assigned namespace in their kubeconfig once:

```bash
kubectl config set-context --current --namespace team-inference
```

Daily commands use that namespace automatically. Members do not create Namespace resources or install a controller. The platform prepares each instance's controller and authorized storage when it is deployed. See [model storage](../docs/features/model-storage.md#share-model-files-across-namespaces) for administrator-selected shared directories.

## Deploy and operate model services

Run from the repository checkout prepared in the [Quick Start](../README.md). Deploy one frontend and all models rendered by a Kustomize root.

See the [multi-model example](../examples/quickstart/multi-model/README.md) for resources and [model storage](../docs/features/model-storage.md) for directory or PVC configuration. Use `examples/quickstart/qwen3-0.6b` for a single model.

```bash
foretoken deploy examples/quickstart/multi-model --name team-models --timeout 20m
```

The command creates or updates the `team-models` deployment instance in the current kubeconfig namespace. Each instance has its own controller and services. Deploy the same directory with another `--name` to run an independent instance alongside it; updates, stop and deletion affect only the selected instance.

Without `--name`, the instance takes the name of the configuration's single FrontendService: `multi-model-frontend` for this example. The frontend uses the instance name in either case. Ordinary deployments use installed applications without building source; use `-e PATH` only for source development.

While waiting, the command shows service status and Pod/container logs. It completes when active services are Ready, zero-capacity services have stopped, and selected alerts are configured. See [service observability](../examples/feature/observability/README.md) for alert configuration.

`--timeout` limits the CLI's waits for cluster operations and service readiness; the default is ten minutes. Source compilation runs separately. Each model's `spec.timeouts.startup` sets its own preparation and startup limits.

If preparation fails or readiness times out, the command reports the cause and leaves the deployed services in Kubernetes. Recovery continues in the cluster; use `status` below to follow progress and inspect logs. For configuration errors, update the Kustomize files and rerun `deploy`.

Inspect the same deployment without applying it:

```bash
foretoken status team-models
```

Status includes Kubernetes waiting and termination messages, including the previous container attempt after a restart. List instances in the current namespace, or watch the selected instance's status and Pod/container logs until Ctrl+C:

```bash
foretoken status
foretoken status team-models --watch
```

Commands also accept the Kustomize path. For a named instance, pass `--name team-models` with that path, for example `foretoken status examples/quickstart/multi-model --name team-models`.

Resolve the public frontend URL after deployment:

```bash
FORETOKEN_FRONTEND_URL="$(foretoken endpoint team-models)"
```

For an HTTP Gateway, resolve its request `Host` separately:

```bash
FORETOKEN_REQUEST_HOST="$(foretoken endpoint team-models --host)"
```

`--host` returns the host and optional port for direct access, or the configured routing hostname for an HTTP Gateway. `foretoken endpoint` waits for the LoadBalancer or Gateway address; use `foretoken deploy` to wait for the services to become ready.

### Stop and resume

```bash
foretoken stop team-models
```

Use this to release GPUs between experiments while keeping configuration, model-cache references and runtime state. The command waits for this instance's frontend and model execution to stop. Resume with `foretoken deploy examples/quickstart/multi-model --name team-models`; model processes start and load weights again.

## Measure serving performance

Use `foretoken perf` to measure response latency and request or token throughput. Pass a Kustomize directory, or `--url` with `--model` for an existing endpoint. Add `--name team-models` to measure the named instance above; `eval` uses the same selection. Choose a workload in [Performance examples](../benchmarks/docs/perf/README.md).

## Evaluate and compare models

Use `foretoken eval` to score model answers with lm-evaluation-harness or EvalScope. It accepts the same service selection options; task and scoring parameters use the selected framework's syntax. See [Quality evaluation](../benchmarks/docs/eval/README.md). Add `--reference` to [compare a candidate's probabilities against a reference](../benchmarks/docs/eval/distribution-comparison.md).

## Export figures

Use `--output plot` to export figures during a benchmark, or `foretoken plot RESULT_DIR [RESULT_DIR ...]` to redraw or compare saved results. See [plotting examples](../benchmarks/docs/recipes.md#plot-saved-results) for metrics, layout and output locations.

Add [`--sync_wandb`](../benchmarks/docs/perf/wandb.md#resume-an-upload) to upload saved results to W&B.

## Find execution bottlenecks

Add `--profile` to `foretoken deploy` or `foretoken perf` to record CPU/GPU execution, then browse captures with `foretoken profile view`. Setup and commands are in [Profiling](../benchmarks/docs/profile/README.md).

## Clean up

Delete the deployed services:

```bash
foretoken delete team-models
```

The platform administrator can run `foretoken uninstall` when the shared platform is no longer needed.

Deletion waits for the selected instance's services and workloads to retire, then removes its controller and instance resources. It does not delete the Kubernetes namespace, neighboring instances or administrator-provided storage. Managed model downloads are collected after their last service reference is released; `stop` retains those references. Dynamic volumes follow their configured retention and StorageClass reclaim policies.

Uninstall preserves CRDs, log storage, and reused cluster components. Managed LeaderWorkerSet and MetalLB controllers are also retained while workloads still depend on them.
