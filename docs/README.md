<!-- SPDX-License-Identifier: Apache-2.0 -->
<!-- SPDX-FileCopyrightText: Copyright contributors to the Foretoken project -->

# Foretoken documentation

English | [简体中文](README_zh.md)

Foretoken orchestrates generative inference on Kubernetes and supports recursive self-improvement (RSI): agents investigate inference systems, change code or configuration, and use measurements to guide further improvements.

## Get started

| Goal | Start here |
| --- | --- |
| Deploy a model service and send a request | [Quick Start](../README.md#quick-start) |
| Have agents research and improve an inference system | [Start an RSI task](../rsi-playbooks/README.md#start-an-optimization-task) |

## RSI optimization

Define the model, workloads, objectives, available resources, and allowed changes. Agents research approaches, implement and deploy candidates, evaluate performance and answer quality, and use profiles and experiment results to choose the next iteration. The task produces retained changes, measurements, and a report explaining the outcome.

| Task | Guide |
| --- | --- |
| Define objectives, resources, and deliverables | [Task template](../rsi-playbooks/templates/task-template.md) |
| Choose an example task | [Optimization tasks](../rsi-playbooks/tasks/README.md) |
| Investigate mechanisms and develop an approach | [Research methods and technical knowledge](../rsi-playbooks/guidance/README.md) |
| Record experiments, compare results, and maintain the report | [Experiment records](../rsi-playbooks/experiment-records.md) |

## User guide

### Installation and deployment

Choose the guide for the environment where the service will run:

| Environment or task | Guide |
| --- | --- |
| Single-host GPU cluster | [k3d](deployment/k3d.md) |
| Local CPU development cluster | [kind](deployment/kind.md) |
| Existing Kubernetes cluster | [Kubernetes](deployment/kubernetes.md) |
| MetaX GPUs | [Platform preparation](deployment/metax-platform.md) · [Model deployment](deployment/metax.md) |
| Build and deploy source changes, including parallel candidates | [Source deployment](deployment/source.md) |

### Model configuration

- [Model sources](features/model-sources.md): Hugging Face, ModelScope, or local model files.
- [Model storage](features/model-storage.md): persistent caches, shared directories, and distribution between nodes.
- [Inference parameters](features/inference-parameters.md): engine arguments, placement, parallelism, and concurrency.

Runnable deployment configurations are maintained in [examples](../examples/), including [model recipes](../examples/recipes/README.md).

### Monitoring and operations

Use the [observability guide](../observability/README.md) to inspect metrics, query persistent logs, and configure alerts and notifications.

## Evaluation and profiling

Use the same measurement tools for standalone service evaluation and RSI experiments. The [evaluation guide](../benchmarks/README.md) covers service selection and saved results.

| Goal | Guide |
| --- | --- |
| Measure latency, throughput, and capacity under load | [Performance benchmarks](../benchmarks/docs/perf/README.md) |
| Score model answers or compare candidate and reference models | [Quality evaluation](../benchmarks/docs/eval/README.md) |
| Find CPU/GPU execution bottlenecks | [Profiling](../benchmarks/docs/profile/README.md) |
| Run workload sweeps and compare results | [Experiment recipes](../benchmarks/docs/recipes.md) |

## Reference

- [CLI](../cli/README.md): supported commands, purposes, and examples.
- [Frontend APIs](../data-plane/frontend/README.md): model requests, supported endpoints, and admission settings.
- [Performance metrics](../benchmarks/docs/metrics.md): metric definitions and units.
- [Helm values](../deploy/charts/foretoken/values.yaml): installation and runtime configuration.

<a id="develop-and-maintain-foretoken"></a>

## Developer guide

Start with [Contributing](../CONTRIBUTING.md) and the [code style](development/code-style.md).

- [Preview and build the documentation](development/documentation.md) when updating guides or navigation.
- Validate changes with the [testing guidelines](development/testing.md) and [CI guidelines](development/ci.md).
- Maintain deployment and storage behavior using the [source-deployment](development/source-deployment.md) and [runtime-cache](development/runtime-cache.md) guides.
- Build and publish versions with the [release guide](development/release.md).
