<!--
SPDX-License-Identifier: Apache-2.0
SPDX-FileCopyrightText: Copyright contributors to the Foretoken project
-->

# Observability

English | [简体中文](README_zh.md)

Observability helps operators determine whether a service is healthy, why requests are slow, whether capacity is sufficient, and which layer is responsible for a problem. Foretoken observability covers continuous monitoring, alerting, and on-demand performance diagnosis.

- **Continuous monitoring**: Prometheus collects Frontend, model-server, GPU, and Kubernetes metrics, while Grafana provides queries and dashboards.
- **Alerting**: Prometheus evaluates alert rules, and Alertmanager groups, suppresses, and delivers notifications.
- **Profiling**: PyTorch Profiler, Nsight Systems, and Nsight Compute diagnose operator, kernel, communication, and CPU/GPU bottlenecks on demand.

Continuous monitoring and alerting require Prometheus Operator in the cluster. kube-prometheus-stack is one optional installation method; an existing Prometheus Operator can be reused.

## Prepare Prometheus

If the cluster already runs Prometheus Operator, skip the installation and continue with Foretoken metric collection.

Otherwise, install kube-prometheus-stack with the values included in this repository:

```bash
helm repo add prometheus-community \
  https://prometheus-community.github.io/helm-charts
helm repo update

kubectl create namespace monitoring \
  --dry-run=client -o yaml | kubectl apply -f -

helm upgrade --install kube-prometheus-stack \
  prometheus-community/kube-prometheus-stack \
  --namespace monitoring \
  --values deploy/observability/kube-prometheus-stack-values.yaml \
  --wait \
  --debug
```

The example values look for Foretoken ServiceMonitors in the `foretoken-platform` namespace. If the platform uses another namespace, copy the file and update `serviceMonitorNamespaceSelector`.

Label the namespace that runs Prometheus. This is a one-time cluster setting:

```bash
kubectl label namespace monitoring \
  inference.foretoken.io/metrics-scraper=true \
  --overwrite
```

The label allows that namespace to reach the model-server's internal HTTP port. NetworkPolicy cannot allow only the `/metrics` path, so a labeled namespace must contain only trusted monitoring services.

## Enable Foretoken metric collection

Enable ServiceMonitors in the Foretoken values:

```yaml
observability:
  serviceMonitor:
    enabled: true
```

The ServiceMonitors discover labeled Frontend and model-server Services across workload namespaces. The platform Prometheus must:

- search the Foretoken control-plane namespace for ServiceMonitors;
- select ServiceMonitors labeled `app.kubernetes.io/name=foretoken-control-plane`.

The included kube-prometheus-stack values configure both selectors. When reusing an existing Prometheus installation, a platform administrator configures them once; model services do not repeat this setup.

ServiceMonitors inherit the Prometheus scrape interval and timeout. Add overrides only when Foretoken needs different values:

```yaml
observability:
  serviceMonitor:
    enabled: true
    interval: 30s
    scrapeTimeout: 10s
```

## Verify metric collection

Confirm that the Foretoken ServiceMonitors exist:

```bash
kubectl get servicemonitor -A \
  -l app.kubernetes.io/name=foretoken-control-plane \
  -o wide
```

Forward the Prometheus Service:

```bash
kubectl --namespace monitoring port-forward \
  service/kube-prometheus-stack-prometheus 9090:9090
```

In another terminal, list the Foretoken targets:

```bash
curl -sS 'http://127.0.0.1:9090/api/v1/targets?state=active' \
  | jq -r '.data.activeTargets[]
      | select((.scrapePool // "") | test("control-plane-(frontend|model-server)/"))
      | [.labels.job, .labels.namespace, .labels.pod, .health, .lastError]
      | @tsv'
```

An `UP` target means Prometheus can reach `/metrics`; it does not mean the model is ready for inference. A failing or restarting Pod cannot be scraped successfully.

## Metric sources

| Component | Endpoint | Contents |
| --- | --- | --- |
| Frontend | `GET /metrics` | HTTP requests, admission queues, routing, and other Frontend runtime metrics |
| model-server | `GET /metrics` | Complete native metrics from the active inference backend |
| model-server | `GET /v1/internal/telemetry` | Versioned JSON snapshot read directly by Foretoken routing and autoscaling |

Prometheus is for observation only and is not part of the Foretoken routing or autoscaling control loop. For the current vLLM adapter, use the `HELP` and `TYPE` metadata in the `/metrics` response as the source of truth for metric names, units, and labels.

GPU and Kubernetes state come from existing platform data sources:

| Signal | Source |
| --- | --- |
| NVIDIA GPU utilization, memory, power, temperature, links, and hardware errors | DCGM Exporter |
| Other accelerator hardware | The vendor's accelerator exporter |
| Container CPU, memory, filesystem, and network | kubelet/cAdvisor |
| Kubernetes object state | kube-state-metrics or the owning controller |

## Alerts and profiling

Prometheus evaluates alert rules and sends notifications through Alertmanager. Alerts use the time series collected from `/metrics`; they do not read the internal Foretoken telemetry endpoint.

Profiling diagnoses a specific experiment or incident rather than providing continuous monitoring. PyTorch Profiler analyzes model execution and operator time, Nsight Systems analyzes process, kernel, and communication timelines, and Nsight Compute performs detailed analysis of individual GPU kernels. Store profiling results with the corresponding benchmark model, concurrency, hardware, and runtime parameters.
