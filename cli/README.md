<!--
SPDX-License-Identifier: Apache-2.0
SPDX-FileCopyrightText: Copyright contributors to the Foretoken project
-->

# Foretoken CLI

English | [简体中文](README_zh.md)

The Foretoken CLI deploys Kustomize configurations, reports serving readiness, and exposes the benchmark command through one `foretoken` entry point.

Install the deployment and status commands from the repository root:

```bash
pip install .
```

Deploy one frontend and all models rendered by a Kustomize root:

```bash
foretoken deploy -k examples/multi-model-quickstart
```

The command applies the configuration, reports each `FrontendService` and `ModelService` state when it changes, and exits when every resource is Ready for its current generation. Change the default ten-minute deadline with `--timeout`.

Inspect the same deployment without applying it:

```bash
foretoken status -k examples/multi-model-quickstart
```

Inspect every Foretoken service in a namespace, or continue watching state changes:

```bash
foretoken status -n foretoken-multi-model-demo
foretoken status -n foretoken-multi-model-demo --watch
```

Benchmark support uses optional dependencies:

```bash
pip install '.[bench]'
foretoken bench --deploy examples/quickstart
```

The CLI uses the active `kubectl` context and honors standard Kubernetes configuration such as `KUBECONFIG`.
