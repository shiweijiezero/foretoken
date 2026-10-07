<!--
SPDX-License-Identifier: Apache-2.0
SPDX-FileCopyrightText: Copyright contributors to the Foretoken project
-->

# Observability

English | [简体中文](README_zh.md)

Use Grafana to inspect serving performance, query persistent logs, and investigate alerts. Platform installation enables metrics and log collection; alerts are opt-in. For a first deployment, follow the [Quick Start](../README.md#quick-start).

## View dashboards

Open Grafana through your cluster's monitoring entry point. The CLI-managed Grafana Service is `foretoken-prometheus-grafana` in `foretoken-platform`, on port 80. It defaults to `ClusterIP`; access from outside the cluster requires an entry point configured by the cluster administrator. Reused Grafana installations keep their existing access settings.

Open Foretoken System Overview, or Foretoken 系统概览 for Chinese. Select a namespace, then a model for inference metrics or a frontend for HTTP traffic and admission. Instance, execution-role, and engine-rank filters narrow backend details; whole-model totals remain visible.

The dashboard starts with the last 15 minutes. Change the time range to inspect historical trends; overview values correspond to the range's end.

| Question | Where to look |
| --- | --- |
| Is the model keeping up with demand? | Prompt/output token rates, completed requests, and running/waiting queues. |
| Where is latency increasing? | First-token and end-to-end latency, output-token intervals, and queue/prefill/decode durations. |
| Is speculative decoding helping? | Compare draft acceptance, output throughput, latency, and automatically collected draft/target GPU time. |
| Are caches or devices under pressure? | Cache occupancy and hit rates, filesystem space, GPU utilization and memory, and CPU/memory usage. |
| How are requests and replicas distributed? | Routing selection shares within each model and role, and autoscaling recommendations versus applied replicas. |
| Why are requests waiting or being rejected? | Admission results, queue wait, and each frontend replica's occupancy and limits. |

TTFT is first-token latency; E2EL is completion latency. TPOT is the average output-token interval per request; ITL measures individual intervals. Units are shown on each panel.

In Admission, select a frontend Pod for replica details and expand the results or resources rows.

## Query logs

In Grafana Explore, select Foretoken Logs and a time range. For the Quick Start namespace:

```logql
{job="foretoken", namespace="foretoken-demo"}
```

Narrow the query with `pod`, `container`, `node`, or `stream`. Append `|~ "(?i)error"` to find errors, or `|= "request-id"` with the identifier you are investigating.

Collection includes model servers and their inference engines, frontends, KV services, and the controller. Collected logs remain queryable after a serving Pod or its namespace is deleted.

## Alerts

Select rules in a `ModelService` or `FrontendService` and redeploy its configuration. The [alert reference](runbooks/alerts.md) covers available rules, thresholds, and response actions; the [service observability example](../examples/observability/README.md) provides a runnable deployment.

To receive notifications, connect a [Lark](integrations/lark/README.md), [Slack](integrations/slack/README.md), or [DingTalk](integrations/dingtalk/README.md) receiver.

## Platform settings

For a source installation, run platform updates from the checkout root with `-e .`, retaining registry settings and any `--engine-source` bindings. Reapply the original install command after upgrading Foretoken to update dashboards and telemetry together.

### Grafana login

CLI-managed Grafana allows anonymous viewing by default. To require login for dashboards and log queries:

```bash
foretoken install --grafana-auth password
```

Use `--grafana-auth anonymous` to restore anonymous viewing. Subsequent installs retain the choice. Reused Grafana remains under its existing administrator's control.

Retrieve the initial administrator credentials:

```bash
kubectl get secret --namespace foretoken-platform \
  foretoken-prometheus-grafana --output json \
  | python3 -c 'import base64,json,sys; d=json.load(sys.stdin)["data"]; print("User:",base64.b64decode(d["admin-user"]).decode()); print("Password:",base64.b64decode(d["admin-password"]).decode())'
```

If the password has been changed in Grafana, use the updated password.

### Log storage

Managed logs retain 14 days of data and start with a 5 GiB volume from the default StorageClass. For 30-day retention and automatic growth up to 50 GiB, save this in `deploy/platform-values.yaml`:

```yaml
observability:
  logs:
    retention: 720h
    maxSize: 50Gi
```

Apply the file again whenever its settings change:

```bash
foretoken install --values deploy/platform-values.yaml
```

Setting `maxSize` enables expansion at 80% usage, doubling the requested capacity up to the limit. The storage driver must support online expansion and per-volume usage statistics.

| Option under `observability.logs` | Use |
| --- | --- |
| `storageClass` / `initialSize` | Select the StorageClass and initial capacity for new storage. Existing volumes retain their capacity. |
| `endpoint` | Use an existing Loki HTTP(S) base URL reachable by collectors and Grafana. |
| `enabled: false` | Stop managed collection while keeping historical queries available. |

### Existing monitoring

The installer reuses compatible Prometheus and GPU exporters. If several Prometheus instances are available, select one explicitly. This example uses `monitoring/prometheus`; replace both names with those of your installation:

```bash
kubectl label namespace monitoring inference.foretoken.io/metrics-scraper=true --overwrite
foretoken install --prometheus monitoring/prometheus
```

The selected Prometheus must select Foretoken's ServiceMonitors and recording rules in `foretoken-platform`. For service alerts, its `ruleNamespaceSelector` must also include workload namespaces. GPU exporters must cover the GPU nodes and expose Pod and namespace labels to associate devices with model workloads.

For automatic Grafana provisioning, watch dashboard ConfigMaps labeled `grafana_dashboard=1` and datasource ConfigMaps labeled `grafana_datasource=1`, both in `foretoken-platform`. For manual dashboard import, export the installed version:

```bash
kubectl get configmap --namespace foretoken-platform \
  foretoken-control-plane-system-dashboard \
  --output jsonpath='{.data.foretoken-system-overview\.json}' \
  > /tmp/foretoken-system-overview.json
```

Import this file into Grafana and select the matching Prometheus datasource. For logs, add a Loki datasource named Foretoken Logs pointing to the configured endpoint; managed Loki uses `http://foretoken-loki.foretoken-platform.svc:3100`.

## Investigate missing data

Check the selected namespace, model, and time range first, and send requests to the service. In Prometheus, inspect Targets for scrape failures and Rules for the `foretoken.recording` group. These resources identify the installed scrape and rule configuration:

```bash
kubectl get servicemonitor,prometheusrule -A \
  -l app.kubernetes.io/name=foretoken-control-plane
```

For a triggered alert, use its [alert reference](runbooks/alerts.md). To investigate execution inside the model process, use [Profiling](../benchmarks/docs/profile/README.md).

## Cleanup

Delete model and frontend services before running `foretoken uninstall`. Uninstall removes the platform and its managed monitoring, collectors, and Loki, while preserving log storage and reused installations. Reinstall with the original command and log settings to query retained logs again.
