<!--
SPDX-License-Identifier: Apache-2.0
SPDX-FileCopyrightText: Copyright contributors to the Foretoken project
-->

# Foretoken CLI

English | [简体中文](README_zh.md)

The Foretoken CLI installs the platform, deploys Kustomize configurations, reports serving readiness, resolves frontend endpoints, and exposes benchmarks through one `foretoken` entry point.

Install the Foretoken CLI with pip:

```bash
pip install -e .
```

Or create and activate a virtual environment with uv:

```bash
uv venv
source .venv/bin/activate
uv pip install -e .
```

This step only installs the `foretoken` command in the current Python environment; it does not change the Kubernetes cluster. The CLI installs the Foretoken Chart version that matches its package version; run `foretoken --version` to see that release version.

Use the CLI to install the platform into the active Kubernetes context with the default local access mode. CLI-managed platform resources use the `foretoken-platform` namespace. Run the same command again to update the existing installation:

```bash
foretoken install
```

The command reuses one compatible Prometheus instance or installs a CLI-managed monitoring stack when none exists. On NVIDIA GPU nodes it reuses one ready DCGM Exporter with a working ServiceMonitor, or installs a CLI-managed exporter when none exists. A shared Prometheus must select that ServiceMonitor. Duplicate, unhealthy, incomplete, or unmonitored exporters stop installation instead of being overlaid. On MetaX GPU nodes the CLI requires and reuses one platform-managed mxExporter with a working ServiceMonitor. The CLI never installs GPU drivers, device plugins, or vendor operators. Use `--prometheus NAMESPACE/NAME` only when automatic discovery finds multiple compatible instances.

Gateway mode requires an existing Gateway Controller. Without existing Gateway details, the CLI creates a dedicated `GatewayClass` and `Gateway`:

```bash
foretoken install --frontend-mode gateway
```

To reuse an existing Gateway instead:

```bash
foretoken install \
  --frontend-mode gateway \
  --gateway-name inference-gateway \
  --gateway-namespace gateway-system \
  --gateway-section-name https
```

Use `--dry-run` to validate and show the installation plan without changing the cluster. Repeatable `--values` files provide platform image, runtime, and hardware settings. Releases originally installed directly with Helm remain under their existing Helm lifecycle and are not adopted automatically.

Deploy one frontend and all models rendered by a Kustomize root:

```bash
foretoken deploy examples/multi-model-quickstart
```

The command applies the configuration, reports each `FrontendService` and `ModelService` state when it changes, and exits when every resource is Ready for its current generation. Change the default ten-minute deadline with `--timeout`.

Delete the resources rendered by the same configuration:

```bash
foretoken delete examples/multi-model-quickstart
```

The command waits for deletion and ignores resources that are already absent. After deleting all Foretoken services, remove the platform release:

```bash
foretoken uninstall
```

The command preserves Foretoken CRDs and refuses to uninstall while user-owned services remain.

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
FORETOKEN_FRONTEND_URL="$(foretoken endpoint examples/quickstart)"
FORETOKEN_REQUEST_HOST="$(foretoken endpoint examples/quickstart --host)"
```

The host value is the URL authority for direct access or the configured routing hostname for an HTTP Gateway. The command waits for the LoadBalancer or Gateway address, but serving readiness remains owned by `foretoken deploy`.

Install the optional benchmark dependencies with pip:

```bash
pip install -e '.[bench]'
```

Or install the benchmark dependencies in the activated uv environment:

```bash
uv pip install -e '.[bench]'
```

Then run the benchmark:

```bash
foretoken bench examples/quickstart
```

The CLI uses the active `kubectl` context and honors standard Kubernetes configuration such as `KUBECONFIG`.
