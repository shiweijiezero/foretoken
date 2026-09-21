<!--
SPDX-License-Identifier: Apache-2.0
SPDX-FileCopyrightText: Copyright contributors to the Foretoken project
-->

# Observability

English | [简体中文](README_zh.md)

Foretoken collects service and accelerator metrics with Prometheus and shows them in the Foretoken System Overview Grafana dashboard. Alert rules are optional and disabled by default.

## Get started

```bash
foretoken install
foretoken deploy examples/quickstart
```

`foretoken install` reuses a Prometheus that already exists in the cluster or installs a CLI-managed kube-prometheus-stack. If it installs the monitoring stack, use the CLI-managed Grafana and retrieve its generated administrator credentials. If it reuses an existing stack, use that platform's Grafana and credentials:

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

In Grafana, open Foretoken System Overview for English or Foretoken 系统概览 for Chinese. Select a namespace and model, then narrow to a model instance, execution role or engine rank. Model-serving, cache, GPU and routing panels follow that selection. Routing decisions show each backend's share within its model and role; a backend is one model instance and data-parallel rank. The selected backend lines keep the full model-and-role denominator.

Shared frontend panels show all traffic through the selected frontend, not just one model. Autoscaling follows the selected model and service; control-plane diagnostics describe the platform.

After upgrading Foretoken, run `foretoken install` again to update the controller, frontend, scrape configuration, and dashboards. Importing dashboard JSON alone does not update metric producers.

## Check that collection works

```bash
kubectl get servicemonitor,prometheusrule -A \
  -l app.kubernetes.io/name=foretoken-control-plane
```

In Prometheus, confirm on Targets that the Foretoken targets are `UP` and on Rules that `foretoken.recording` is loaded. If service alerts are enabled, also confirm the corresponding `foretoken.alerting` rules are loaded. This query returns the Frontend request rate:

```promql
sum(foretoken:frontend_http_response_starts:rate5m)
```

## Use an existing monitoring stack

The CLI reuses what the cluster already provides and installs only what is missing:

| Component | Not present | Present | Present but not usable | `foretoken uninstall` |
| --- | --- | --- | --- | --- |
| Prometheus | Install a CLI-managed kube-prometheus-stack | Reuse it | Stop and ask for an explicit choice | Remove only the CLI-managed release |
| NVIDIA DCGM Exporter | Install a CLI-managed exporter on clusters with NVIDIA GPUs | Reuse it | Stop | Remove only the CLI-managed release |
| MetaX mxExporter | Install a CLI-managed exporter | Reuse it | Stop | Remove only CLI-managed resources |

An exporter is usable when it covers every GPU node and the selected Prometheus scrapes it. The CLI does not install GPU drivers, device plugins, or vendor operators.

If several compatible Prometheus instances exist, choose one:

```bash
# Allow the Prometheus namespace to scrape Foretoken metrics
kubectl label namespace monitoring \
  inference.foretoken.io/metrics-scraper=true \
  --overwrite

# Select the Prometheus instance
foretoken install --prometheus monitoring/prometheus
```

GPU panels and alerts identify devices by the Foretoken model-group and model-role Pod labels. The CLI-managed DCGM Exporter publishes them; a reused exporter needs the same labels, otherwise those panels stay empty.

For service alerts, a reused Prometheus must select rules in the workload namespaces through `ruleNamespaceSelector`; the CLI-managed stack already does this.

With a reused Prometheus, Grafana stays under that platform's control. A Grafana sidecar that watches ConfigMaps labeled `grafana_dashboard=1` picks up the dashboard from the `foretoken-platform` namespace. Otherwise, export the JSON and import it through Grafana:

```bash
kubectl get configmap \
  --namespace foretoken-platform \
  foretoken-control-plane-system-dashboard \
  --output jsonpath='{.data.foretoken-system-overview\.json}' \
  > /tmp/foretoken-system-overview.json
```

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

Selecting the power alert also requires a positive `spec.observability.alerts.thresholds.nvidiaPowerWatts`, chosen for the GPU model. Setting a threshold alone does not enable a rule. Configure notifications with a [Lark](integrations/lark/README.md) or [Slack](integrations/slack/README.md) receiver.

## Metrics reference

| Source | Contents |
| --- | --- |
| Frontend `/metrics` | HTTP requests, admission queues, routing, and runtime state |
| model-server `/metrics` | Inference-engine metrics and RuntimeCache filesystem state |
| Controller `/metrics` | Reconciliation, workqueues, and published autoscaling decisions |
| DCGM Exporter | NVIDIA utilization, memory, power, temperature, and XID errors |
| mxExporter | MetaX utilization and memory |
| kubelet/cAdvisor | Container CPU and memory |

Dashboard latency metrics use seconds for TTFT and E2EL, and milliseconds for TPOT and ITL. The p50/p95/p99 percentiles combine request histograms across the selected instances, separately for each model and role. Request rates, token throughput and scheduler pressure are shown by backend, where a backend is one model instance and data-parallel rank; the same panels retain the model-level totals in their summary tiles. Prefix-cache hit ratios divide total hit tokens by total queried tokens; idle or missing observations have no ratio. Routing shares count selection decisions, not completed requests or cache hits.

The following recording rules remain available for alerts and fixed-window queries. Model-serving rules are derived from vLLM metrics.

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

After all Foretoken services are deleted, `foretoken uninstall` removes CLI-managed Prometheus, DCGM Exporter, and MetaX mxExporter resources. Reused installations are left unchanged.
