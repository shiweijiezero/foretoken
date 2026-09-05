<!--
SPDX-License-Identifier: Apache-2.0
SPDX-FileCopyrightText: Copyright contributors to the Foretoken project
-->

# Observability

English | [简体中文](README_zh.md)

Foretoken installs Prometheus collection and recording rules for service and accelerator metrics. It does not install Foretoken alert rules. Alert thresholds, Alertmanager routing, and notifications remain owned by the platform team.

## Install collection

```bash
foretoken install
```

The CLI discovers the collection path and prints its plan before changing the cluster.

| Component | No suitable existing instance | Qualified existing instance | Conflict or incomplete path | `foretoken uninstall` |
| --- | --- | --- | --- | --- |
| Prometheus | Install a CLI-managed kube-prometheus-stack | Reuse it | Stop and request an explicit selection or repair | Remove only a CLI-managed release |
| NVIDIA DCGM Exporter | Install a CLI-managed exporter when NVIDIA GPUs exist | Reuse it | Stop | Remove only a CLI-managed release |
| MetaX mxExporter | Stop; the cluster must provide it | Reuse it | Stop | Preserve it |

A qualified exporter is ready, covers every selected GPU node, and has exactly one ServiceMonitor that its Prometheus selects. The CLI does not install GPU drivers, device plugins, or vendor operators.

If automatic discovery finds multiple compatible Prometheus instances, select one explicitly:

```bash
# Allow the Prometheus namespace to scrape Foretoken metrics
kubectl label namespace monitoring \
  inference.foretoken.io/metrics-scraper=true \
  --overwrite

# Select the Prometheus instance
foretoken install --prometheus monitoring/prometheus
```

The Prometheus platform owns this namespace label and removes it when collection is no longer needed.

## Verify collection

```bash
# List the Foretoken monitors and recording rules
kubectl get servicemonitor,prometheusrule -A \
  -l app.kubernetes.io/name=foretoken-control-plane

# For CLI-managed Prometheus, open the Prometheus UI locally
kubectl port-forward \
  --namespace foretoken-platform \
  service/foretoken-prometheus-kube-prometheus \
  9090:9090
```

Open <http://127.0.0.1:9090/targets> and confirm Foretoken targets are `UP`. Open <http://127.0.0.1:9090/rules> and confirm `foretoken.recording` is loaded. Reused Prometheus instances use their platform-provided access path.

A minimal query for frontend request volume is:

```promql
sum(foretoken:frontend_http_response_starts:rate5m)
```

## Metrics and recording rules

| Source | Contents |
| --- | --- |
| Frontend `/metrics` | HTTP requests, admission queues, routing, and runtime state |
| model-server `/metrics` | Native metrics from the active inference backend |
| DCGM Exporter | NVIDIA utilization, memory, power, temperature, and XID errors |
| mxExporter | MetaX utilization and memory metrics |
| kubelet/cAdvisor | Container CPU, memory, filesystem, and network |
| kube-state-metrics | Kubernetes object state |

The following stable recording rules are currently derived from Frontend metrics and vLLM model-server metric families. They are not a normalized metrics contract for other inference backends.

| Recording rule | Meaning |
| --- | --- |
| `foretoken:frontend_http_response_starts:rate5m` | Frontend HTTP response starts per second |
| `foretoken:frontend_http_response_start_5xx_ratio:rate5m` | Response-start 5xx ratio, not inference failure ratio |
| `foretoken:model_server_prompt_tokens:rate5m` | vLLM prompt tokens per second |
| `foretoken:model_server_generation_tokens:rate5m` | vLLM generated tokens per second |
| `foretoken:model_server_requests_running:sum` | vLLM requests currently running |
| `foretoken:model_server_requests_waiting:sum` | vLLM requests waiting in the scheduler |
| `foretoken:model_server_kv_cache_usage_ratio:max` | Highest vLLM KV-cache usage ratio |

Rules preserve namespace, Frontend service, model group, model role, model name, and optional Prefill/Decode pipeline scope. Counter rules calculate reset-aware five-minute rates before aggregation. For raw backend metric names, units, and labels, inspect the backend `/metrics` `HELP` and `TYPE` metadata.

A response may begin with `2xx` and fail later while streaming. Do not use `foretoken:frontend_http_response_start_5xx_ratio:rate5m` as an inference-success SLO.

## Alerts and profiling

Foretoken currently provides metrics and recording rules, not alert rules. Define alert thresholds and notification policy in the Prometheus and Alertmanager configuration owned by the platform team.

Foretoken does not manage a profiling workflow. For a reproducible investigation, run a controlled workload and use PyTorch Profiler, Nsight Systems, or Nsight Compute through the model runtime and hardware platform. Profiling changes serving performance; record the model, load, hardware, and runtime settings with the result.

## Remove collection

After all Foretoken services are deleted, `foretoken uninstall` removes CLI-managed Prometheus and DCGM Exporter releases. Reused Prometheus, DCGM Exporter, and mxExporter installations remain unchanged.
