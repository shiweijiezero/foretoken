<!--
SPDX-License-Identifier: Apache-2.0
SPDX-FileCopyrightText: Copyright contributors to the Foretoken project
-->

# Foretoken command-line tool

English | [简体中文](README_zh.md)

The Foretoken command-line tool installs the shared Kubernetes platform, deploys model services from Kustomize configurations, reports serving readiness, resolves frontend endpoints, and runs benchmarks through one `foretoken` entry point.

For a new cluster, start by installing the command-line tool. If `foretoken --version` already works, go straight to platform installation. If the cluster already has the Foretoken platform, start with model deployment.

## Before you start

You need Python 3.10 or later, an active Kubernetes context, `kubectl`, and Helm. GPU nodes must already have their vendor driver and Kubernetes device plugin. Source installation also requires Docker and Make, plus either a local kind/k3d cluster or an OCI registry reachable by every target node.

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

This step only installs the `foretoken` command in the current Python environment; it does not change the Kubernetes cluster. Run `foretoken --version` to see the command-line tool and corresponding platform version.

## Install the Kubernetes platform

`foretoken install` installs the Foretoken CRDs and controller in the active Kubernetes context. Platform resources use the `foretoken-platform` namespace. The command also configures monitoring and, in Gateway mode, the Gateway resources. Deploy model services separately with `foretoken deploy`.

### Default installation

The default uses release images and local access through a `LoadBalancer` Service:

```bash
foretoken install
```

During installation, the command-line tool discovers Prometheus and accelerator metric exporters. It reuses compatible shared instances, installs managed Prometheus and NVIDIA DCGM Exporter releases when needed, and connects to the mxExporter already provided by a MetaX cluster. See [Observability](../observability/README.md) for monitoring selection and configuration.

### Gateway mode

The command-line tool creates a dedicated `GatewayClass` and `Gateway` only when the cluster runs Envoy Gateway:

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

Build Foretoken images from the current source tree and configure the platform to use them:

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

Repeatable `--values` files provide platform image, runtime, and hardware settings. Release and source installs record their mode in Helm metadata and cannot switch silently. Releases originally installed directly with Helm remain under their existing Helm lifecycle and are not adopted automatically.

### Persistent runtime cache

Create one `RuntimeCache` in a workload namespace to let Foretoken provision and manage a shared cache PVC. Existing PVCs remain supported through `workload.cache.claimName`. See [Persistent Runtime Cache](../docs/development/runtime-cache.md).

## Deploy and operate model services

Deploy one frontend and all models rendered by a Kustomize root. The multi-model example needs up to four GPUs, 12 CPU cores, 100 GiB memory, and a default `ReadWriteMany` StorageClass with online expansion; use `examples/quickstart` for the smallest path.

```bash
foretoken deploy examples/multi-model-quickstart
```

The command applies the configuration, reports each `FrontendService` and `ModelService` state when it changes, and exits when every service reports Ready for its current configuration. Change the default ten-minute deadline with `--timeout`.

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

## Run benchmarks

Install the optional benchmark dependencies with pip:

```bash
pip install 'foretoken[bench]'

# For source installation from the repository:
# pip install -e .
# pip install -e '.[bench]'
```

Or install the benchmark dependencies in the activated uv environment:

```bash
uv pip install 'foretoken[bench]'
```

Then run the benchmark:

```bash
foretoken bench examples/multi-model-quickstart --model Qwen/Qwen3-0.6B
```

The command-line tool uses the active `kubectl` context and honors standard Kubernetes configuration such as `KUBECONFIG`.

## Clean up

Delete the resources rendered by the same configuration:

```bash
foretoken delete examples/multi-model-quickstart
```

The command waits for deletion and ignores resources that are already absent. After deleting all Foretoken services, remove the platform release:

```bash
foretoken uninstall
```

The command preserves Foretoken CRDs and refuses to uninstall while user-owned services remain. It removes monitoring and Gateway resources managed by the command-line tool with the platform, while reused cluster components remain unchanged.
