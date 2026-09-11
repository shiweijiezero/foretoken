<!--
SPDX-License-Identifier: Apache-2.0
SPDX-FileCopyrightText: Copyright contributors to the Foretoken project
-->

# Observability

English | [简体中文](README_zh.md)

Foretoken collects service and accelerator metrics with Prometheus, shows them in the **Foretoken System Overview** Grafana dashboard, and installs alert rules for the most common problems.

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

In Prometheus, confirm on **Targets** that the Foretoken targets are `UP` and on **Rules** that `foretoken.recording` and `foretoken.alerting` are loaded. This query returns the Frontend request rate:

```promql
sum(foretoken:frontend_http_response_starts:rate5m)
```

## Use an existing monitoring stack

The CLI reuses what the cluster already provides and installs only what is missing:

| Component | Not present | Present | Present but not usable | `foretoken uninstall` |
| --- | --- | --- | --- | --- |
| Prometheus | Install a CLI-managed kube-prometheus-stack | Reuse it | Stop and ask for an explicit choice | Remove only the CLI-managed release |
| NVIDIA DCGM Exporter | Install a CLI-managed exporter on clusters with NVIDIA GPUs | Reuse it | Stop | Remove only the CLI-managed release |
| MetaX mxExporter | Stop; the cluster must provide it | Reuse it | Stop | Keep it |

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

With a reused Prometheus, Grafana stays under that platform's control. A Grafana sidecar that watches ConfigMaps labeled `grafana_dashboard=1` picks up the dashboard from the `foretoken-platform` namespace. Otherwise, export the JSON and import it through Grafana:

```bash
kubectl get configmap \
  --namespace foretoken-platform \
  foretoken-control-plane-system-dashboard \
  --output jsonpath='{.data.foretoken-system-overview\.json}' \
  > /tmp/foretoken-system-overview.json
```

## Alerts

Alert rules are installed together with collection. Each alert links to its entry in the [runbooks](runbooks/alerts.md), which explain the signal and how to investigate it. The dashboard draws each alert threshold as a dashed line on the matching panel.

To change thresholds or the notification language, edit `observability.yaml` in the [observability example](../examples/observability/README.md) and pass it to the installation:

```bash
foretoken install --values examples/observability/observability.yaml
```

`language` accepts `zh`, `en`, or `bilingual` and applies to all alerts of the installation. Notifications are delivered by the cluster's Alertmanager; the optional [Lark integration](integrations/lark/README.md) adds a receiver for Lark group bots.

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

## One-off profiling (experimental, source build)

On an already prepared diagnostic ModelService, request one Torch capture:

```bash
foretoken profile MODEL_SERVICE -n foretoken-diagnostic --duration 15s
```

This command does not send inference requests. Run a small, authorized workload through the service's normal frontend while the command reports `Capturing`. The runtime stops automatically and the command prints the retained artifact PVC and path. Ctrl-C requests cancellation; losing the terminal connection or reaching `--timeout` only stops local observation, not the runtime's deadline. The default capture duration is 15 seconds; the default CLI wait is 10 minutes, including export.

Platform preparation is a one-time operator task, independent of `observability.mode` and alerting:

1. Prepare an otherwise empty diagnostic namespace and a dedicated artifact PVC there. All participating Pods must be able to write it; use shared storage with `ReadWriteMany` for Pods across nodes. Do not reuse the model cache or KV-store claim.
2. Set the namespace and existing claim in a copy of [`deploy/profiling-values.example.yaml`](../deploy/profiling-values.example.yaml), and pass it to a [source installation](../docs/custom-deployment.md) with `foretoken install -e . --values YOUR_VALUES_FILE`. The CRDs, controller and model-server image must come from the same source. Supply the registry option required by your cluster. Enabling this binding changes ModelGroup Pod templates, so do it before deploying diagnostic services, not on an occupied shared namespace.
3. Deploy a diagnostic ModelService into that namespace and wait for it to be Ready. The operator grants the caller Kubernetes permission to create/get/patch ProfileRuns; an inference token alone is insufficient. Each capture thereafter needs only the command above, with no service YAML changes or manual port-forward.

Artifacts are uncompressed `.pt.trace.json` files plus a manifest on the PVC. Access them through your platform's storage access and open the trace in [Perfetto](https://ui.perfetto.dev/); the command does not download files. Captures without the expected worker traces and GPU kernel activity fail instead of reporting success. Keep the workload small: profiling adds overhead and trace files can be large, as explained in [vLLM's profiling guide](https://docs.vllm.ai/en/stable/contributing/profiling/).

The experimentally validated configuration is a single-worker NVIDIA service using vLLM 0.26.0. `bench --profile`, delay, sampling limits, repeated windows, Nsight and MetaX are not available. Multi-worker capture and injected native-utility or storage failures still need hardware validation. Native profiling failure can terminate the selected diagnostic runtime; use a service where that interruption is acceptable. Unconfirmed runtime shutdown retains the ProfileRun finalizer and recovery plan for operator diagnosis. See the [profiling design](../docs/development/profiling.md) for lifecycle and recovery details.

## Remove collection

After all Foretoken services are deleted, `foretoken uninstall` removes the CLI-managed Prometheus and DCGM Exporter releases. Reused Prometheus, DCGM Exporter, and mxExporter installations are left unchanged.
