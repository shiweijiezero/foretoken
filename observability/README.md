<!--
SPDX-License-Identifier: Apache-2.0
SPDX-FileCopyrightText: Copyright contributors to the Foretoken project
-->

# Observability

English | [简体中文](README_zh.md)

Foretoken collects service and accelerator metrics with Prometheus and shows them in the **Foretoken System Overview** Grafana dashboard. Alert rules are optional and disabled by default.

## Get started

```bash
foretoken install
foretoken deploy examples/quickstart
```

`foretoken install` reuses a Prometheus that already exists in the cluster or installs a CLI-managed kube-prometheus-stack. The CLI-managed Grafana loads the dashboard automatically. Retrieve its generated administrator credentials, then open Grafana at the address your cluster provides:

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
```

In Grafana, select **Dashboards** and open **Foretoken System Overview**. It follows a request through the Frontend, model serving, caches, and accelerators, and ends with autoscaling decisions; routing and control-plane details are in collapsed sections. Filters narrow the view to a namespace, Frontend service, model group, model role, model, or model service.

## Check that collection works

```bash
kubectl get servicemonitor,prometheusrule -A \
  -l app.kubernetes.io/name=foretoken-control-plane
```

In Prometheus, confirm on Targets that the Foretoken targets are `UP` and on Rules that `foretoken.recording`, `foretoken.accelerator-recording`, and `foretoken.alerting` are loaded. This query returns the Frontend request rate:

```promql
sum(foretoken:frontend_http_response_starts:rate5m)
```

## Install monitoring

Run `foretoken install` to detect the cluster monitoring stack and configure the collection required by Foretoken. When MetaX GPU resources are detected, the CLI verifies the compatible mxExporter and its metrics collection. After installation, open Prometheus Targets and confirm Foretoken targets are `UP`; the CLI-managed Grafana loads the Foretoken System Overview dashboard.

## Alerts

Alert selection belongs to the service deployment. In a `ModelService`, select only the rules needed for that model:

```yaml
spec:
  observability:
    alerts:
      rules:
        - ForetokenMetricsTargetDown
```

The [observability example](../examples/observability/README.md) keeps these settings in a Kustomize patch for the Quick Start. Edit its `observability.yaml`, then deploy:

```bash
foretoken deploy examples/observability --timeout 20m
```

`FrontendService` uses the same selection path for frontend scrape and HTTP errors. Model rules cover only that ModelService's execution groups; shared frontend failures remain frontend-level signals. Available names and trigger conditions are in the [alert reference](runbooks/alerts.md).

Remove a name, or use `rules: []`, and deploy again to remove the corresponding alerts. Metrics and the dashboard remain available. The CLI reports alert configuration failures separately from serving readiness; `deploy` does not install monitoring.

Selecting the power alert also requires a positive `spec.observability.alerts.thresholds.nvidiaPowerWatts`, chosen for the GPU model. Setting a threshold alone does not enable a rule. Notification language, destination, and time zone are configured on the receiver; see the optional [Lark integration](integrations/lark/README.md).

## Metrics reference

| Source | Contents |
| --- | --- |
| Frontend `/metrics` | HTTP requests, admission queues, routing, and runtime state |
| model-server `/metrics` | Inference-engine metrics and RuntimeCache filesystem state |
| Controller `/metrics` | Reconciliation, workqueues, and published autoscaling decisions |
| DCGM Exporter | NVIDIA utilization, memory, power, temperature, and XID errors |
| mxExporter | MetaX utilization and memory |
| kubelet/cAdvisor | Container CPU and memory |

The dashboard and alerts query these recording rules. Model-serving rules are derived from vLLM metrics.

| Area | Recording rule | Meaning |
| --- | --- | --- |
| Frontend | `foretoken:frontend_up:sum` | Reporting Frontend targets |
| Frontend | `foretoken:frontend_http_response_starts:rate5m` | HTTP response starts per second |
| Frontend | `foretoken:frontend_http_response_start_5xx_ratio:rate5m` | Share of response starts with a 5xx status |
| Frontend | `foretoken:frontend_http_response_start_latency_seconds:quantile5m` | Time until response headers are sent, as `p50`, `p90`, and `p99` |
| Frontend | `foretoken:frontend_upstream_queued_requests:sum` | Requests waiting for admission, by scaling target |
| Frontend | `foretoken:frontend_kv_index_source_health_ratio:min` | Lowest KV event-source health ratio across Frontend replicas |
| Model serving | `foretoken:model_server_up:sum` | Reporting model-server targets |
| Model serving | `foretoken:model_server_completed_requests:rate5m` | Completed requests per second, by finish reason |
| Model serving | `foretoken:model_server_prompt_tokens:rate5m` | Prompt tokens per second |
| Model serving | `foretoken:model_server_generation_tokens:rate5m` | Generated tokens per second |
| Model serving | `foretoken:model_server_requests_running:sum` | Requests currently running |
| Model serving | `foretoken:model_server_requests_waiting:sum` | Requests waiting in the scheduler |
| Model serving | `foretoken:model_server_e2e_request_latency_seconds:quantile5m` | Time from Frontend handler entry to generation completion, as `p50`, `p90`, and `p99` |
| Model serving | `foretoken:model_server_time_to_first_token_seconds:quantile5m` | Time to first token, as `p50`, `p90`, and `p99` |
| Model serving | `foretoken:model_server_time_per_output_token_seconds:quantile5m` | Time per output token, as `p50`, `p90`, and `p99` |
| Model serving | `foretoken:model_server_inter_token_latency_seconds:quantile5m` | Gap between consecutive output tokens, as `p50`, `p90`, and `p99` |
| Model serving | `foretoken:model_server_request_stage_time_seconds:quantile5m` | Time spent in the `queue`, `prefill`, and `decode` stages, as `p50`, `p90`, and `p99` |
| Model serving | `foretoken:model_server_preemptions:rate5m` | Requests preempted per second |
| Model serving | `foretoken:model_server_request_prompt_tokens_bucket:rate5m` | Prompt length histogram buckets |
| Model serving | `foretoken:model_server_request_generation_tokens_bucket:rate5m` | Output length histogram buckets |
| Cache | `foretoken:model_server_kv_cache_usage_ratio:max` | Highest KV cache usage ratio in an engine |
| Cache | `foretoken:model_server_prefix_cache_hit_ratio:rate5m` | Local or external prefix cache hit ratio |
| Cache | `foretoken:model_server_runtime_cache_available_bytes:min` | Lowest RuntimeCache free space |
| Cache | `foretoken:model_server_runtime_cache_usage_ratio:max` | Highest RuntimeCache usage ratio |
| Cache | `foretoken:model_server_runtime_cache_observation_success:min` | Whether every RuntimeCache mount can be inspected |
| Cache | `foretoken:model_server_runtime_cache_temporary:max` | Whether any model server uses temporary Pod-local cache storage |
| Accelerator | `foretoken:accelerator_gpu_utilization_ratio` | Per-device NVIDIA or MetaX utilization |
| Accelerator | `foretoken:accelerator_gpu_memory_usage_ratio` | Per-device NVIDIA or MetaX memory usage |
| Accelerator | `foretoken:accelerator_gpu_power_watts` | Per-device NVIDIA power draw |
| Accelerator | `foretoken:accelerator_gpu_temperature_celsius` | Per-device NVIDIA temperature |

Rules keep the namespace, Frontend service, model group, model role, model name, and Prefill/Decode pipeline scope labels. Frontend latency ends when response headers are sent, so for streaming responses it does not include token delivery; generation completion latency and TTFT start when the Frontend handler begins after JSON decoding. These cross-process measurements require synchronized node clocks. A streaming response can start with `2xx` and fail later, so the 5xx ratio is not an inference success rate. Accelerator rules cover only devices used by Foretoken workloads.

## Profiling

For a short CPU/GPU capture on an existing diagnostic service, see [Profiling](profiling.md). It is separate from metrics collection.

## Remove collection

After all Foretoken services are deleted, `foretoken uninstall` removes the CLI-managed Prometheus and DCGM Exporter releases. Reused Prometheus, DCGM Exporter, and mxExporter installations are left unchanged.
