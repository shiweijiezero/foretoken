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

The cluster needs Prometheus Operator. Reuse an existing installation when available. Otherwise, install kube-prometheus-stack with the values included in this repository:

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

Allow the monitoring namespace to reach the model-server metrics port:

```bash
kubectl label namespace monitoring \
  inference.foretoken.io/metrics-scraper=true \
  --overwrite
```

Enable ServiceMonitors when installing Foretoken:

```yaml
observability:
  serviceMonitor:
    enabled: true
```

ServiceMonitors inherit the Prometheus scrape interval and timeout. Set `interval` or `scrapeTimeout` only when Foretoken needs an override.

The included kube-prometheus-stack values look for Foretoken ServiceMonitors in the `foretoken-platform` namespace. If the platform uses another namespace, copy the file and update `serviceMonitorNamespaceSelector`.

## Confirm collection is enabled

```bash
kubectl get pods --namespace monitoring

kubectl get servicemonitor -A \
  -l app.kubernetes.io/name=foretoken-control-plane
```

The Prometheus, Grafana, and Alertmanager Pods should be `Running`, and Foretoken ServiceMonitors should be listed for the enabled components. If metrics are missing, inspect the ServiceMonitor, Service labels, named ports, and the Pod's `/metrics` endpoint as troubleshooting steps rather than part of the normal workflow.

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
