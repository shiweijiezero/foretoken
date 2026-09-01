<!--
SPDX-License-Identifier: Apache-2.0
SPDX-FileCopyrightText: Copyright contributors to the Foretoken project
-->

# Observability

English | [简体中文](README_zh.md)

Observability helps operators understand whether a service is healthy, why requests are slow, whether capacity is sufficient, and which layer is responsible for a problem. Foretoken supports this work through metrics, alerts, and on-demand profiling.

- **Metrics and dashboards**: Prometheus collects runtime metrics continuously, and Grafana provides queries and dashboards.
- **Alerts**: Prometheus detects sustained abnormal conditions, while Alertmanager handles notification, grouping, and silencing.
- **Profiling**: PyTorch Profiler and Nsight diagnose CPU, GPU, kernel, and communication bottlenecks while reproducing a problem.

## Enable metric collection

Frontend and model-server expose `/metrics` without requiring Prometheus. The normal installation connects them automatically:

```bash
foretoken install
```

The command reuses the unique compatible Prometheus instance in the cluster. If none exists, it installs a CLI-managed kube-prometheus-stack release. NVIDIA GPU nodes also receive a DCGM Exporter when none exists. One ready exporter that covers every GPU node is reused only when it has a working ServiceMonitor selected by Prometheus. Duplicate, unhealthy, incomplete, or unmonitored exporters stop installation instead of being overlaid. Mixed CPU/GPU clusters must identify GPU nodes with `nvidia.com/gpu.present=true` or `feature.node.kubernetes.io/pci-10de.present=true`; the CLI does not guess node placement or install drivers.

When more than one compatible Prometheus instance exists, select one explicitly:

```bash
kubectl label namespace monitoring \
  inference.foretoken.io/metrics-scraper=true \
  --overwrite

foretoken install --prometheus monitoring/prometheus
```

The CLI configures namespace access for its managed stack, plus the ServiceMonitors and recording rules. The label on a reused Prometheus namespace remains platform-owned and must be removed by that platform owner when no longer needed. A release originally installed directly with Helm remains under that Helm lifecycle and is not adopted automatically.

## Confirm collection is enabled

```bash
kubectl get servicemonitor,prometheusrule -A \
  -l app.kubernetes.io/name=foretoken-control-plane
```

For the CLI-managed Prometheus, open the service locally:

```bash
kubectl port-forward \
  --namespace foretoken-platform \
  service/foretoken-prometheus-kube-prometheus \
  9090:9090
```

Confirm that Foretoken targets are `UP` at <http://127.0.0.1:9090/targets> and that `foretoken.recording` is loaded at <http://127.0.0.1:9090/rules>. For a reused Prometheus, use its existing access path for the same checks.

## Remove the integration

`foretoken uninstall` removes CLI-managed Prometheus and DCGM Exporter releases after all Foretoken services are deleted. Reused installations are preserved.

## Recording rules

The optional PrometheusRule provides stable, low-cardinality queries over the raw Frontend and model-server series:

| Recording rule | Meaning |
| --- | --- |
| `foretoken:frontend_http_response_starts:rate5m` | Frontend HTTP responses started per second, split by method, handler, and status class |
| `foretoken:frontend_http_response_start_5xx_ratio:rate5m` | Frontend response-start 5xx ratio; not an inference failure ratio |
| `foretoken:model_server_prompt_tokens:rate5m` | Prompt tokens processed per second |
| `foretoken:model_server_generation_tokens:rate5m` | Generation tokens processed per second |
| `foretoken:model_server_requests_running:sum` | Requests currently running |
| `foretoken:model_server_requests_waiting:sum` | Requests currently waiting in the vLLM scheduler |
| `foretoken:model_server_kv_cache_usage_ratio:max` | Highest KV-cache usage ratio |

The rules retain the namespace, Frontend service, model group and role, model name, and optional Prefill/Decode pipeline scope. Counter rules apply a reset-aware five-minute rate before aggregation. Missing model-server samples remain absent, while an observed gauge value of zero remains zero.

Frontend HTTP status is recorded when the response starts. A later streaming failure can still have status `2xx`, so the response-start 5xx ratio is not a user-visible inference success SLO.

## Metric sources

| Source | Contents |
| --- | --- |
| Frontend `/metrics` | HTTP requests, admission queues, routing, and Frontend runtime state |
| model-server `/metrics` | Complete native metrics from the active inference backend |
| DCGM Exporter | NVIDIA GPU utilization, memory, power, temperature, and hardware errors |
| kubelet/cAdvisor | Container CPU, memory, filesystem, and network |
| kube-state-metrics | Kubernetes object state |

The model-server does not rename or filter backend-native metrics. For the current vLLM adapter, use the `HELP` and `TYPE` metadata in the `/metrics` response as the source of truth for metric names, units, and labels.

Prometheus is for observation only and is not part of the Foretoken routing or autoscaling control loop. Routing and autoscaling continue to read the versioned internal model-server snapshot directly.

## Alerts

Prometheus evaluates alert rules and sends notifications through Alertmanager. Alerts should represent sustained conditions that require action, such as unavailable services, elevated error rates, persistent queue pressure, abnormal latency, or exhausted capacity. Notification receivers and routing are configured once in the platform Alertmanager.

## Profiling

Profiling diagnoses a specific experiment or incident rather than providing continuous monitoring, and it should not be enabled by default:

- PyTorch Profiler analyzes model execution, operator time, and memory;
- Nsight Systems analyzes process, kernel, and communication timelines;
- Nsight Compute performs detailed analysis of individual GPU kernels.

Profiling affects inference performance. Send only a small reproducible workload and store the results with the model, concurrency, hardware, and runtime parameters.
