<!--
SPDX-License-Identifier: Apache-2.0
SPDX-FileCopyrightText: Copyright contributors to the Foretoken project
-->

# Observability

English | [简体中文](README_zh.md)

Foretoken uses metrics, alerts, and on-demand profiling to show service health, request latency, queue pressure, capacity, and hardware utilization.

## Collect metrics

Install Foretoken to configure Prometheus collection and recording rules:

```bash
foretoken install
```

The CLI reuses the single compatible Prometheus instance in the cluster. If none exists, it installs a managed kube-prometheus-stack release.

GPU metric collection depends on the device platform:

| Device | CLI behavior |
| --- | --- |
| NVIDIA | Configure DCGM Exporter automatically |
| MetaX | Connect the official [mxExporter](https://github.com/MetaX-MACA/mxExporter) already provided by the cluster |

An existing exporter must be ready, cover the GPU nodes, and have a ServiceMonitor selected by Prometheus. Installation stops when exporters conflict or the collection path is incomplete. In mixed CPU/GPU clusters, label NVIDIA GPU nodes with `nvidia.com/gpu.present=true` or `feature.node.kubernetes.io/pci-10de.present=true`. GPU drivers, device plugins, and vendor operators remain platform-owned.

Only select Prometheus explicitly when automatic discovery reports multiple compatible instances. First allow the Prometheus namespace to collect Foretoken metrics, then identify the instance:

```bash
kubectl label namespace monitoring \
  inference.foretoken.io/metrics-scraper=true \
  --overwrite

foretoken install --prometheus monitoring/prometheus
```

The namespace label remains owned by the platform that operates the reused Prometheus. Remove it through that platform when collection is no longer required.

## Verify collection

List the Foretoken monitors and recording rules:

```bash
kubectl get servicemonitor,prometheusrule -A \
  -l app.kubernetes.io/name=foretoken-control-plane
```

For the CLI-managed Prometheus, forward its service to the local machine:

```bash
kubectl port-forward \
  --namespace foretoken-platform \
  service/foretoken-prometheus-kube-prometheus \
  9090:9090
```

Open <http://127.0.0.1:9090/targets> and confirm that the Foretoken targets are `UP`. Then open <http://127.0.0.1:9090/rules> and confirm that `foretoken.recording` is loaded. When reusing Prometheus, perform the same checks through its existing access path.

## Metric sources

| Source | Contents |
| --- | --- |
| Frontend `/metrics` | HTTP requests, admission queues, routing, and Frontend runtime state |
| model-server `/metrics` | Native metrics from the active inference backend |
| DCGM Exporter | NVIDIA GPU utilization, memory, power, temperature, and hardware errors |
| mxExporter | MetaX GPU utilization and memory metrics |
| kubelet/cAdvisor | Container CPU, memory, filesystem, and network |
| kube-state-metrics | Kubernetes object state |

For model-server metrics, use the `HELP` and `TYPE` metadata in `/metrics` as the source of truth for names, units, and labels.

## Recording rules

The Foretoken `PrometheusRule` provides stable, low-cardinality queries over Frontend and model-server metrics:

| Recording rule | Meaning |
| --- | --- |
| `foretoken:frontend_http_response_starts:rate5m` | Frontend HTTP responses started per second, split by method, handler, and status class |
| `foretoken:frontend_http_response_start_5xx_ratio:rate5m` | Frontend response-start 5xx ratio; not an inference failure ratio |
| `foretoken:model_server_prompt_tokens:rate5m` | Prompt tokens processed per second |
| `foretoken:model_server_generation_tokens:rate5m` | Generation tokens processed per second |
| `foretoken:model_server_requests_running:sum` | Requests currently running |
| `foretoken:model_server_requests_waiting:sum` | Requests currently waiting in the vLLM scheduler |
| `foretoken:model_server_kv_cache_usage_ratio:max` | Highest KV-cache usage ratio |

The rules preserve namespace, Frontend service, model group and role, model name, and optional Prefill/Decode pipeline scope. Counter rules calculate a reset-aware five-minute rate before aggregation.

Frontend HTTP status is recorded when the response starts. A later streaming failure can still have status `2xx`, so the response-start 5xx ratio is not an inference success SLO.

## Alerts

Prometheus evaluates sustained conditions such as service unavailability, elevated errors, queue pressure, abnormal latency, and exhausted capacity. Alertmanager owns notification receivers, grouping, and routing.

## Profiling

Use profiling to investigate a reproducible experiment or incident, not for continuous monitoring:

- PyTorch Profiler analyzes model execution, operator time, and memory;
- Nsight Systems analyzes process, kernel, and communication timelines;
- Nsight Compute analyzes individual GPU kernels.

Profiling affects inference performance. Use a small workload and record the model, concurrency, hardware, and runtime parameters with the result.

## Remove collection

After all Foretoken services are deleted, `foretoken uninstall` removes CLI-managed Prometheus and DCGM Exporter releases. Reused installations remain unchanged.
