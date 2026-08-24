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

Frontend and model-server expose `/metrics` without requiring Prometheus. To collect those metrics and evaluate the recording rules, reuse an existing Prometheus Operator or install kube-prometheus-stack:

```bash
helm repo add prometheus-community \
  https://prometheus-community.github.io/helm-charts
helm repo update

helm upgrade --install kube-prometheus-stack \
  prometheus-community/kube-prometheus-stack \
  --namespace monitoring \
  --create-namespace \
  --values deploy/observability/kube-prometheus-stack-values.yaml \
  --wait
```

Label the namespace that runs the Prometheus Pods so it can reach model-server metrics:

```bash
PROMETHEUS_NAMESPACE=monitoring
kubectl label namespace "${PROMETHEUS_NAMESPACE}" \
  inference.foretoken.io/metrics-scraper=true \
  --overwrite
```

After the Operator CRDs are established, a normal online Foretoken installation uses the default `auto` mode to create the ServiceMonitors and PrometheusRule:

```bash
helm upgrade --install foretoken \
  oci://ghcr.io/shiweijiezero/foretoken/charts/foretoken \
  --namespace foretoken-platform \
  --create-namespace \
  --set frontend.enabled=true \
  --wait
```

For an existing release, apply the integration through the Helm lifecycle that owns it:

```bash
helm upgrade foretoken \
  oci://ghcr.io/shiweijiezero/foretoken/charts/foretoken \
  --namespace foretoken-platform \
  --reuse-values \
  --set observability.mode=enabled \
  --wait
```

Use `disabled` in the same command to remove the Chart-owned monitoring resources. `--reuse-values` applies only to releases that already use `observability.mode`; older releases must replace `observability.serviceMonitor` in their values file before upgrading.

`auto` creates both resources only when the cluster exposes the `ServiceMonitor` and `PrometheusRule` APIs. Offline rendering and GitOps should use `enabled` and install the Operator CRDs before Foretoken. Set `interval` or `scrapeTimeout` only when overriding Prometheus defaults.

The included kube-prometheus-stack values select Foretoken resources from the `foretoken-platform` namespace. When reusing another Prometheus installation, its ServiceMonitor and rule namespace selectors must include the Foretoken release namespace. If its object selectors require platform-specific labels, add the same labels to all Foretoken monitoring resources:

```yaml
observability:
  mode: enabled
  additionalLabels:
    release: your-prometheus-release
```

## Confirm collection is enabled

First confirm that Kubernetes contains the expected resources:

```bash
kubectl get servicemonitor,prometheusrule -A \
  -l app.kubernetes.io/name=foretoken-control-plane
```

For the included kube-prometheus-stack installation, open Prometheus locally:

```bash
kubectl port-forward \
  --namespace monitoring \
  service/kube-prometheus-stack-prometheus \
  9090:9090
```

In Prometheus, confirm that the Foretoken targets are `UP` at <http://127.0.0.1:9090/targets> and that `foretoken.recording` is loaded at <http://127.0.0.1:9090/rules>. After the service receives traffic, query a recorded metric:

```bash
curl --get http://127.0.0.1:9090/api/v1/query \
  --data-urlencode 'query=foretoken:model_server_requests_running:sum'
```

Object presence alone does not prove that Prometheus selected the monitors or loaded the rules. For an existing platform installation, use its normal Prometheus access path for the same target, rule, and query checks.

## Disable or remove the integration

Setting `observability.mode=disabled` or uninstalling Foretoken removes only the ServiceMonitors and PrometheusRule owned by the Foretoken release. It does not uninstall Prometheus, Prometheus Operator, Grafana, or Alertmanager. Remove the namespace label only when no remaining Foretoken workload needs that Prometheus installation:

```bash
kubectl label namespace monitoring \
  inference.foretoken.io/metrics-scraper-
```

Replace `monitoring` when Prometheus runs in another namespace.

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
