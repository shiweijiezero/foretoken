<!-- SPDX-License-Identifier: Apache-2.0 -->
<!-- SPDX-FileCopyrightText: Copyright contributors to the Foretoken project -->

# Foretoken documentation

English | [简体中文](README_zh.md)

Start with the [Quick Start](../README.md#quick-start) to deploy a model and send a request. Use the guides below to choose a deployment environment, configure services, or contribute changes.

## Deploy Foretoken

| Task | Guide |
| --- | --- |
| Create a single-host GPU cluster | [k3d](deployment/k3d.md) |
| Create a local CPU development cluster | [kind](deployment/kind.md) |
| Use an existing Kubernetes cluster | [Kubernetes](deployment/kubernetes.md) |
| Prepare a MetaX cluster and run a model | [Platform setup](deployment/metax-platform.md) · [Model deployment](deployment/metax.md) |
| Build and deploy local source changes | [Source deployment](deployment/source.md) |

## Configure and use services

| Task | Guide |
| --- | --- |
| Choose a model provider or local files | [Model sources](features/model-sources.md) |
| Configure persistent model storage | [Model storage](features/model-storage.md) |
| Set engine options, placement, parallelism, and concurrency | [Inference parameters](features/inference-parameters.md) |
| Call model APIs and configure frontend admission | [Frontend](../data-plane/frontend/README.md) |
| Operate services from the command line | [CLI](../cli/README.md) |
| Measure performance or model quality | [Benchmarks](../benchmarks/README.md) |
| Inspect metrics, logs, and alerts | [Observability](../observability/README.md) |

Runnable configurations are maintained in [examples](../examples/).

## Develop and maintain Foretoken

Start with [Contributing](../CONTRIBUTING.md) and the [code style](development/code-style.md).

- Verify changes using the [testing guidelines](development/testing.md) and [CI guidelines](development/ci.md).
- When changing deployment or storage behavior, preserve the responsibilities described in [source deployments](development/source-deployment.md) and [runtime caches](development/runtime-cache.md).
- Follow the [release guide](development/release.md) to choose a version, build artifacts, and publish them.
