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
  prometheusRule:
    enabled: true
    additionalLabels:
      release: kube-prometheus
  grafanaDashboard:
    enabled: true
    additionalLabels:
      grafana_dashboard: "1"
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

The `prometheusRule.additionalLabels` value independently matches the platform
Prometheus `ruleSelector`; its `ruleNamespaceSelector` must select the Helm
release namespace. Both resources are disabled by default, so a cluster without
the Prometheus Operator CRDs can still install the chart.

The existing versioned JSON telemetry endpoints remain the autoscaler's data
source; Prometheus is not part of the autoscaling control loop.

## Recording rules

The optional PrometheusRule turns raw producer series into a stable query layer.
It removes Pod and engine identity while retaining bounded workload dimensions:

| Recording rule | Meaning |
| --- | --- |
| `foretoken:frontend_http_response_starts:rate5m` | Frontend HTTP responses started per second, split by method, handler, and status class |
| `foretoken:frontend_http_response_start_5xx_ratio:rate5m` | Frontend HTTP response-start 5xx fraction; not an inference failure ratio |
| `foretoken:model_server_prompt_tokens:rate5m` | Prompt tokens processed per second across replicas and engines |
| `foretoken:model_server_generation_tokens:rate5m` | Generation tokens processed per second across replicas and engines |
| `foretoken:model_server_requests_running:sum` | Requests currently running across replicas and engines |
| `foretoken:model_server_requests_waiting:sum` | Requests currently waiting in the vLLM scheduler |
| `foretoken:model_server_kv_cache_usage_ratio:max` | Highest KV-cache usage ratio across replicas and engines |

The rules normalize Kubernetes discovery labels to `frontend_service`,
`model_group`, `model_role`, and optional `pd_pipeline_scope`. The scope keeps
the prefill and decode groups of one P/D pipeline correlatable. Counter rules
use a reset-aware five-minute `rate()`. Missing model-server samples remain
missing instead of being converted to zero.

Token counters are rated per raw Pod/engine series before aggregation, so a
single process reset cannot create a negative fleet rate. A new counter series
needs at least two samples; one missed scrape can still be evaluated from the
remaining five-minute window. Real gauge zero remains zero, while an entirely
missing input remains absent. Target liveness continues to use the raw `up`
series.

Frontend HTTP status and duration are observed when the response object starts.
A later streaming failure can still have status `2xx`, so the response-start
5xx ratio must not be used as a user-visible inference success SLO. Native vLLM
latency histograms are intentionally left out of this first rule set until their
measurement boundaries are validated for each API and workflow, or
Foretoken-owned stream-boundary metrics are exposed.

## Grafana overview dashboard

The optional dashboard ConfigMap is selected by an existing Grafana sidecar.
The default `grafana_dashboard: "1"` label matches kube-prometheus-stack. If the
platform uses another selector, replace `grafanaDashboard.additionalLabels`.
The sidecar must watch the Foretoken Helm release namespace; the chart does not
install Grafana or create a Prometheus datasource.

The dashboard uses a Prometheus datasource variable instead of a hard-coded
datasource UID. It provides namespace, Frontend service, model role, and model
group filters over six panels:

| Panel | Meaning |
| --- | --- |
| Prometheus scrape targets | Raw `up` state for Frontend and model-server endpoints; not request readiness |
| Frontend HTTP response starts | Response objects started per second, split by bounded route and status labels |
| HTTP response-start 5xx ratio | Per-Frontend 5xx fraction; not an inference failure SLO |
| Engine tokens processed | Prompt and generation tokens per model-server stage; P/D roles are not combined |
| vLLM scheduler stage requests | Running and waiting stage requests; not users or the Frontend admission queue |
| Hottest engine KV-cache usage | Maximum recorded KV-cache ratio, not a fleet average |

Missing samples remain `No data`; the dashboard does not turn missing
model-server series into zero. Latency, user success, GPU, autoscaling, and
tenant panels are excluded until reliable producers exist for those signals.

## Verify the integration

First verify that the Foretoken monitor objects were installed. The active
target check below confirms that Prometheus selected them:

```bash
kubectl get servicemonitor -A \
  -l app.kubernetes.io/name=foretoken-control-plane -o wide

kubectl get servicemonitor -A \
  -l release=kube-prometheus -o wide

kubectl get prometheusrule -A \
  -l app.kubernetes.io/name=foretoken-control-plane -o wide

kubectl get configmap -A -l grafana_dashboard=1 \
  -o custom-columns=NAMESPACE:.metadata.namespace,NAME:.metadata.name
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
  --data-urlencode 'query=foretoken:frontend_http_response_starts:rate5m{namespace="<workload-namespace>"}' | jq

curl -sS -G http://127.0.0.1:9090/api/v1/query \
  --data-urlencode 'query=foretoken:model_server_requests_running:sum{namespace="<workload-namespace>"}' | jq
```

Validate rule syntax and calculations before deploying changes:

```bash
promtool check rules \
  deploy/charts/foretoken/files/recording-rules.yaml

promtool test rules \
  observability/tests/recording-rules.test.yaml

observability/tests/validate-grafana-dashboard.sh

jq -f observability/tests/grafana-dashboard-promql.jq \
  deploy/charts/foretoken/files/grafana/foretoken-overview.json \
  > /tmp/foretoken-grafana-promql.json

promtool check rules /tmp/foretoken-grafana-promql.json
```

An `UP` target means Prometheus successfully scraped `/metrics`; it is not the
same as Foretoken request readiness. A model-server target cannot become `UP`
while its Pod is crash-looping, so make the current model-server workload
healthy before using target health as an integration check. Once its HTTP
server is running, `/metrics` remains available even when admission is closed.
