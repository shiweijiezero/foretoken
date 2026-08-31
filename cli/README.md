<!--
SPDX-License-Identifier: Apache-2.0
SPDX-FileCopyrightText: Copyright contributors to the Foretoken project
-->

# Foretoken CLI

English | [简体中文](README_zh.md)

The Foretoken CLI deploys Kustomize configurations, reports serving readiness, resolves frontend endpoints, and exposes the benchmark command through one `foretoken` entry point.

Install the CLI from the repository root:

```bash
pip install -e .
```

Deploy one frontend and all models rendered by a Kustomize root:

```bash
foretoken deploy examples/multi-model-quickstart
```

The command applies the configuration, reports each `FrontendService` and `ModelService` state when it changes, and exits when every resource is Ready for its current generation. Change the default ten-minute deadline with `--timeout`.

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

To load both the URL and its HTTP `Host` value:

```bash
source <(foretoken endpoint examples/quickstart --format shell)
```

This sets `FORETOKEN_FRONTEND_URL` and `FORETOKEN_REQUEST_HOST`. The latter is the URL authority for direct access or the configured routing hostname for an HTTP Gateway. The command waits for the LoadBalancer or Gateway address, but serving readiness remains owned by `foretoken deploy`.

Benchmark support uses optional dependencies:

```bash
pip install -e '.[bench]'
foretoken bench examples/quickstart
```

The CLI uses the active `kubectl` context and honors standard Kubernetes configuration such as `KUBECONFIG`.
