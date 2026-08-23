<!--
SPDX-License-Identifier: Apache-2.0
SPDX-FileCopyrightText: Copyright contributors to the Foretoken project
-->

# Observability

Foretoken integrates with a platform-provided Prometheus Operator, Grafana, and
Alertmanager. The Foretoken chart does not install another monitoring stack.

## Enable Prometheus discovery

Enable the chart's ServiceMonitors and match the labels selected by the
platform Prometheus instance. For the current kube-prometheus-stack deployment,
use:

```yaml
observability:
  serviceMonitor:
    enabled: true
    additionalLabels:
      release: kube-prometheus
    interval: 30s
    scrapeTimeout: 10s
  prometheus:
    namespace: monitoring
```

`additionalLabels` must match the Prometheus `serviceMonitorSelector`. The
Prometheus `serviceMonitorNamespaceSelector` must also select the namespace in
which the Foretoken Helm release is installed. The
`prometheus.namespace` value is also used to allow metrics traffic through the
model-server NetworkPolicy. That rule trusts the namespace rather than a
specific Prometheus Pod selector, so only trusted monitoring workloads should
run in that namespace.

The existing versioned JSON telemetry endpoints remain the autoscaler's data
source; Prometheus is not part of the autoscaling control loop.

## Verify the integration

First verify that the Foretoken monitor objects were installed. The active
target check below confirms that Prometheus selected them:

```bash
kubectl get servicemonitor -A \
  -l app.kubernetes.io/name=foretoken-control-plane -o wide

kubectl get servicemonitor -A \
  -l release=kube-prometheus -o wide
```

Then inspect the active Prometheus targets. Adjust the namespace and Service
name if the platform uses different names:

```bash
kubectl get --raw \
  '/api/v1/namespaces/monitoring/services/http:kube-prometheus-kube-prome-prometheus:9090/proxy/api/v1/targets?state=active' \
  | jq -r '.data.activeTargets[]
      | select((.scrapePool // "") | test("control-plane-(frontend|model-server)/"))
      | [.labels.job, .labels.namespace, .labels.pod, .health, .lastError]
      | @tsv'
```

For interactive PromQL checks, forward the existing Prometheus Service:

```bash
kubectl -n monitoring port-forward \
  service/kube-prometheus-kube-prome-prometheus 9090:9090
```

In another terminal, replace `<workload-namespace>` and query target health,
Frontend traffic, and model-server scheduler pressure:

```bash
curl -sS -G http://127.0.0.1:9090/api/v1/query \
  --data-urlencode 'query=up{namespace="<workload-namespace>",endpoint=~"http|model-server"}' | jq

curl -sS -G http://127.0.0.1:9090/api/v1/query \
  --data-urlencode 'query=sum by (pod) (rate(http_requests_total{namespace="<workload-namespace>",endpoint="http"}[5m]))' | jq

curl -sS -G http://127.0.0.1:9090/api/v1/query \
  --data-urlencode 'query=sum by (pod) (vllm:num_requests_running{namespace="<workload-namespace>",endpoint="model-server"})' | jq
```

An `UP` target means Prometheus successfully scraped `/metrics`; it is not the
same as Foretoken request readiness. A model-server target cannot become `UP`
while its Pod is crash-looping, so make the current model-server workload
healthy before using target health as an integration check. Once its HTTP
server is running, `/metrics` remains available even when admission is closed.
