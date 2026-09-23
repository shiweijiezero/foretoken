<!--
SPDX-License-Identifier: Apache-2.0
SPDX-FileCopyrightText: Copyright contributors to the Foretoken project
-->

# Foretoken command-line tool

English | [简体中文](README_zh.md)

The Foretoken command-line tool installs the shared Kubernetes platform, deploys model services from Kustomize configurations, reports serving readiness, resolves frontend URLs, and runs benchmarks through one `foretoken` entry point.

## Before you start

You need Python 3.11 or later, an active Kubernetes context, `kubectl`, and Helm. GPU nodes must already have their vendor driver and Kubernetes device plugin.

## Install the command-line tool

Install the published Foretoken command-line tool package with pip:

```bash
pip install foretoken

# For source installation from the repository:
# pip install -e .
```

Or create and activate a virtual environment with uv:

```bash
uv venv
source .venv/bin/activate
uv pip install foretoken
```

Run `foretoken --version` to check the installed command-line tool version.

## Install the Kubernetes platform

`foretoken install` installs the Foretoken CRDs and controller in the active Kubernetes context. Platform resources use the `foretoken-platform` namespace. The command also configures monitoring and, in Gateway mode, the Gateway resources. Deploy model services separately with `foretoken deploy`.

### Default installation

The default uses release images and local access through a `LoadBalancer` Service:

```bash
foretoken install
```

Installation selects the NVIDIA or MetaX runtime and automatically reuses or installs LeaderWorkerSet and the shared RDMA device plugin. Explicit runtime settings in `--values` take precedence; in a mixed-GPU cluster, select a resource with `runtime.vllm.gpu.resourceName` or restrict the nodes with `runtime.vllm.gpu.nodeSelector`.

See [Observability](../observability/README.md) for dashboards and alerts.

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

Prepare the build tools listed in the [source deployment guide](../docs/custom-deployment.md), then build and install from the repository root:

```bash
foretoken install -e .
```

A standard active kind or k3d context imports the built images locally. Other Kubernetes contexts need a registry reachable by their nodes. Sign in to the registry host with an account that can push the target repository before installation:

```bash
docker login ghcr.io
foretoken install -e . --registry ghcr.io/example/foretoken
```

Registry login authorizes the local image push. Private registries also need `imagePullSecrets` and `workload.imagePullSecrets` through `--values` so nodes can pull the images; see [Deploy Foretoken from Source](../docs/custom-deployment.md).

### Installation options

Use `--values` only to override platform image, runtime, or hardware settings. Without an override, installation compares supported public sources for default platform images and OCI charts. Use `--oci-registry` to select a registry explicitly; image references supplied through values remain unchanged. Source selection runs on the CLI host, so the selected registry must also be reachable from the cluster nodes.

Model services are reached through an IP address outside the cluster. k3d, k3s, and cloud clusters assign one automatically. Clusters built with kubeadm, RKE2, or kubespray have no address assignment by default, so installation there ends with `LoadBalancer support Not verified`. Give Foretoken a range of unused addresses in the nodes' subnet, confirmed with the cluster administrator, and it assigns them to services:

```yaml
loadBalancer:
  managedAddresses:
    - 192.168.1.240-192.168.1.250
```

## Deploy and operate model services

Run from the repository checkout prepared in the [Quick Start](../README.md). Deploy one frontend and all models rendered by a Kustomize root.

See the [multi-model example](../examples/multi-model-quickstart/README.md) for resources and [model storage](../docs/model-storage.md) for directory or PVC configuration. Use `examples/quickstart` for a single model.

```bash
foretoken deploy examples/multi-model-quickstart --timeout 20m
```

The command applies the configuration, reports service state changes, and exits when every service is Ready and its selected alerts are configured. Without `--timeout`, it waits up to ten minutes. Configure service alerts in the Kustomize deployment; see [service observability](../examples/observability/README.md).

Inspect the same deployment without applying it:

```bash
foretoken status examples/multi-model-quickstart
```

Inspect every Foretoken service in a namespace, or continue watching state changes:

```bash
foretoken status -n foretoken-multi-model-demo
foretoken status -n foretoken-multi-model-demo --watch
```

Resolve the public frontend URL after deployment:

```bash
FORETOKEN_FRONTEND_URL="$(foretoken endpoint examples/multi-model-quickstart)"
```

For an HTTP Gateway, resolve its request `Host` separately:

```bash
FORETOKEN_REQUEST_HOST="$(foretoken endpoint examples/multi-model-quickstart --host)"
```

`--host` returns the host and optional port for direct access, or the configured routing hostname for an HTTP Gateway. `foretoken endpoint` waits for the LoadBalancer or Gateway address; use `foretoken deploy` to wait for the services to become ready.

## Evaluate model services

Use `foretoken perf` to measure service performance and `foretoken eval` to score model quality with lm-evaluation-harness or EvalScope. Both accept a Kustomize directory or `--url` for an existing service. Installation, commands, and result settings are in [Model Service Evaluation](../benchmarks/README.md).

## Capture a diagnostic profile

Add `--profile` to `foretoken deploy` or `foretoken perf` to capture performance data. Use `foretoken profile view` to browse results. See [Profiling](../observability/profiling.md).

## Clean up

Delete the deployed services before uninstalling the platform:

```bash
foretoken delete examples/multi-model-quickstart
foretoken uninstall
```

CRDs and reused cluster components are retained. Managed LeaderWorkerSet and MetalLB controllers are also retained while workloads still depend on them.
