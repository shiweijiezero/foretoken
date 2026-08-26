<!--
SPDX-License-Identifier: Apache-2.0
SPDX-FileCopyrightText: Copyright contributors to the Foretoken project
-->

# Observability

English | [简体中文](README_zh.md)

Foretoken exposes `/metrics` from the Frontend and model services so operators can inspect request traffic, queues, routing, backend execution, and cache behavior. Production deployments should connect these endpoints to Prometheus.

## Choose a deployment

### Recommended deployment

If the cluster does not have a monitoring stack, install kube-prometheus-stack. The included values select only the ServiceMonitors created by the Foretoken platform release:

```bash
helm repo add prometheus-community \
  https://prometheus-community.github.io/helm-charts
helm repo update

kubectl create namespace monitoring \
  --dry-run=client -o yaml | kubectl apply -f -

kubectl label namespace monitoring \
  inference.foretoken.io/metrics-scraper=true \
  --overwrite

helm upgrade --install kube-prometheus-stack \
  prometheus-community/kube-prometheus-stack \
  --namespace monitoring \
  --values deploy/observability/kube-prometheus-stack-values.yaml \
  --wait \
  --debug
```

The example values look for Foretoken ServiceMonitors in the `foretoken-platform` namespace. If the platform uses another namespace, copy the file and update `serviceMonitorNamespaceSelector`.

Enable ServiceMonitors when installing Foretoken:

```yaml
observability:
  serviceMonitor:
    enabled: true
```

When the cluster already runs Prometheus Operator, do not install another kube-prometheus-stack. Ensure that the platform Prometheus:

- selects `app.kubernetes.io/name=foretoken-control-plane`;
- searches the Foretoken control-plane namespace for ServiceMonitors;
- runs in a namespace labeled `inference.foretoken.io/metrics-scraper=true`.

These are one-time platform settings and are not repeated for each model service.

### Minimal deployment

For local development, Kind validation, or deployments that do not need centralized monitoring, leave ServiceMonitors disabled:

```yaml
observability:
  serviceMonitor:
    enabled: false
```

The Frontend and model-server still expose `/metrics`, and Foretoken routing and autoscaling continue to use their internal JSON snapshot. The Helm chart simply does not create Prometheus discovery resources.

## Optional scrape settings

ServiceMonitors inherit the platform Prometheus scrape interval and timeout. Add overrides only when Foretoken needs different values:

```yaml
observability:
  serviceMonitor:
    enabled: true
    interval: 30s
    scrapeTimeout: 10s
```

## Verify the integration

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
