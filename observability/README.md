<!--
SPDX-License-Identifier: Apache-2.0
SPDX-FileCopyrightText: Copyright contributors to the Foretoken project
-->

# Observability

English | [简体中文](README_zh.md)

Foretoken collects service and accelerator metrics with Prometheus and displays them in the Foretoken System Overview Dashboard. Recording rules aggregate raw samples into the queries used by the Dashboard. [Alerting](alerting.md) is a separate, optional capability.

## Install collection

```bash
foretoken install
```

The CLI discovers the collection path and prints its plan before changing the cluster. For custom collection settings, edit [`examples/observability/platform.yaml`](../examples/observability/platform.yaml) and pass it with `foretoken install --values examples/observability/platform.yaml`. Model services continue to use their existing example YAML and `foretoken deploy`.

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

List the Foretoken monitors and Prometheus rules:

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

Open <http://127.0.0.1:9090/targets> and confirm that the Foretoken targets are `UP`. Then open <http://127.0.0.1:9090/rules> and confirm that `foretoken.recording` is loaded. When reusing Prometheus, perform the same checks through its existing access path.

A minimal query for frontend request volume is:

```promql
sum(foretoken:frontend_http_response_starts:rate5m)
```

## Open the Grafana dashboard

The CLI-managed kube-prometheus-stack loads the **Foretoken System Overview** automatically. Retrieve its generated administrator credentials and open Grafana locally:

```bash
GRAFANA_USER="$(kubectl get secret \
  --namespace foretoken-platform \
  foretoken-prometheus-grafana \
  --output jsonpath='{.data.admin-user}' | base64 --decode)"
GRAFANA_PASSWORD="$(kubectl get secret \
  --namespace foretoken-platform \
  foretoken-prometheus-grafana \
  --output jsonpath='{.data.admin-password}' | base64 --decode)"
printf 'Grafana user: %s\nGrafana password: %s\n' \
  "$GRAFANA_USER" "$GRAFANA_PASSWORD"

kubectl port-forward \
  --namespace foretoken-platform \
  service/foretoken-prometheus-grafana \
  3000:80
```

Open <http://127.0.0.1:3000>, then select **Dashboards** and open **Foretoken System Overview**. The single dashboard follows the request path from Frontend traffic and admission through model-server latency, throughput, and scheduler state, then shows KV and RuntimeCache behavior, accelerator utilization, and serving-container resources. Shared filters select the workload namespace, Frontend service, model group, model role, model, and autoscaled model service. Routing panels show selection outcomes, candidate counts, and stage latency. Control-plane panels show reconciliation and workqueue health; autoscaling panels compare recommendations with applied and serving capacity, observation age, and decision reasons. Control-plane and accelerator panels cover the platform rather than a single workload namespace.

When Foretoken reuses an existing Prometheus, Grafana remains owned by that platform. A Grafana sidecar that discovers ConfigMaps labeled `grafana_dashboard=1` can load the dashboard from the `foretoken-platform` namespace. Otherwise, extract the JSON and import it through the platform's normal dashboard workflow:

```bash
kubectl get configmap \
  --namespace foretoken-platform \
  foretoken-control-plane-system-dashboard \
  --output jsonpath='{.data.foretoken-system-overview\.json}' \
  > /tmp/foretoken-system-overview.json
```

## Metrics and recording rules

| Source | Contents |
| --- | --- |
| Frontend `/metrics` | HTTP requests, admission queues, routing, and runtime state |
| model-server `/metrics` | Native inference-backend metrics and mounted RuntimeCache filesystem state |
| Controller `/metrics` | Reconciliation, workqueues, and published model-service autoscaling decisions |
| DCGM Exporter | NVIDIA utilization, memory, power, temperature, and XID errors |
| mxExporter | MetaX utilization and memory metrics |
| kubelet/cAdvisor | Container CPU, memory, filesystem, and network |
| kube-state-metrics | Kubernetes object state |

The following stable recording rules provide the query layer used by the system dashboard. Model-serving rules currently derive from vLLM metric families and are not a normalized contract for other inference backends.

| Area | Recording rule | Meaning |
| --- | --- | --- |
| Frontend | `foretoken:frontend_up:sum` | Reporting Frontend targets |
| Frontend | `foretoken:frontend_http_response_starts:rate5m` | HTTP response starts per second |
| Frontend | `foretoken:frontend_http_response_start_5xx_ratio:rate5m` | Response-start 5xx ratio, not inference failure ratio |
| Frontend | `foretoken:frontend_http_response_start_latency_seconds:quantile5m` | Time until the handler produces response headers, with `p50`, `p90`, or `p99`; excludes SSE body delivery |
| Frontend | `foretoken:frontend_upstream_queued_requests:sum` | Requests waiting for admission by scaling target |
| Frontend | `foretoken:frontend_kv_index_source_health_ratio:min` | Lowest KV event-source health ratio across Frontend replicas |
| Model serving | `foretoken:model_server_up:sum` | Reporting model-server targets |
| Model serving | `foretoken:model_server_completed_requests:rate5m` | Completed requests per second by finish reason |
| Model serving | `foretoken:model_server_prompt_tokens:rate5m` | Prompt tokens per second |
| Model serving | `foretoken:model_server_generation_tokens:rate5m` | Generated tokens per second |
| Model serving | `foretoken:model_server_requests_running:sum` | Requests currently running |
| Model serving | `foretoken:model_server_requests_waiting:sum` | Requests waiting in the scheduler |
| Model serving | `foretoken:model_server_e2e_request_latency_seconds:quantile5m` | Complete model-server request/generation latency with `p50`, `p90`, and `p99` series |
| Model serving | `foretoken:model_server_time_to_first_token_seconds:quantile5m` | TTFT with `p50`, `p90`, and `p99` series |
| Model serving | `foretoken:model_server_time_per_output_token_seconds:quantile5m` | TPOT with `p50`, `p90`, and `p99` series |
| Cache | `foretoken:model_server_kv_cache_usage_ratio:max` | Highest in-engine KV Cache usage ratio |
| Cache | `foretoken:model_server_prefix_cache_hit_ratio:rate5m` | Local or external Prefix Cache token hit ratio |
| Cache | `foretoken:model_server_runtime_cache_available_bytes:min` | Lowest reported RuntimeCache available space |
| Cache | `foretoken:model_server_runtime_cache_usage_ratio:max` | Highest reported RuntimeCache filesystem usage ratio |
| Cache | `foretoken:model_server_runtime_cache_observation_success:min` | Whether every reporting RuntimeCache mount can be inspected |
| Cache | `foretoken:model_server_runtime_cache_temporary:max` | Whether any model-server is using Pod-scoped temporary cache storage |
| Accelerator | `foretoken:accelerator_gpu_utilization_ratio` | Per-device NVIDIA or MetaX utilization ratio |
| Accelerator | `foretoken:accelerator_gpu_memory_usage_ratio` | Per-device NVIDIA or MetaX memory utilization ratio |

Rules preserve namespace, Frontend service, model group, model role, model name, and optional Prefill/Decode pipeline scope. Counter rules calculate reset-aware five-minute rates before aggregation. Frontend HTTP duration is measured when the handler returns its response; for SSE this is response-start latency, while the model-server E2E rule measures generation completion from the shared Frontend arrival-time boundary, not client delivery. Dashboard latency summaries show the maximum per-group quantile rather than a quantile pooled across groups. For raw backend metric names, units, and labels, inspect the backend `/metrics` `HELP` and `TYPE` metadata.

A response may begin with `2xx` and fail later while streaming. Do not use `foretoken:frontend_http_response_start_5xx_ratio:rate5m` as an inference-success SLO.

## Diagnose a problem

[Alerting](alerting.md) evaluates sustained unhealthy signals and sends notifications through Alertmanager. Enable it separately and choose thresholds and notification channels in its example configuration.

For an individual slow request, use [distributed tracing](tracing.md).

## Remove collection

After all Foretoken services are deleted, `foretoken uninstall` removes CLI-managed Prometheus and DCGM Exporter releases. Reused Prometheus, DCGM Exporter, and mxExporter installations remain unchanged.
