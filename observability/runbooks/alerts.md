<!--
SPDX-License-Identifier: Apache-2.0
SPDX-FileCopyrightText: Copyright contributors to the Foretoken project
-->

# Service Alerts

English | [简体中文](alerts_zh.md)

Enable alerts for a frontend or model service, then use the notification to locate the affected service or device.

## Enable alerts

Add the selected rules under the service's `spec`, for example:

```yaml
spec:
  observability:
    alerts:
      rules:
        - ForetokenMetricsTargetDown
```

Redeploy the service configuration to apply changes. Remove a rule, or set `rules: []`, to disable it. The [observability example](../../examples/observability/README.md) provides a runnable deployment.

Connect a [Lark](../integrations/lark/README.md), [Slack](../integrations/slack/README.md), or [DingTalk](../integrations/dingtalk/README.md) receiver for notifications. If selected rules do not appear in Prometheus, check the service's `AlertsReady` condition.

## Choose rules

For investigation, open Foretoken System Overview in [Grafana](../README.md#view-dashboards) and select the notification's namespace and frontend or model.

Threshold fields below are relative to `spec.observability.alerts.thresholds`; admission fields are under `admission`. The three admission ratio/latency rules require explicit thresholds and positive minimum rates, with no defaults. Their optional `scope` defaults to `service`, aggregating each model across the selected frontend's Pods; `pod` evaluates each model in each Pod. `window` sets the calculation window (default `1m`), and `for` sets how long the condition must persist before firing (default `5m`). Durations accept whole seconds, minutes, or hours.

Both admission ratios use all completed HTTP admission calls for the model as their denominator, including failures and CPU-only preparation. Calls finish at first backend acceptance, preparation completion, or terminal failure—not at generation completion. They count calls, not candidate work units, and exclude internal calls.

| Rule | Service | Trigger and thresholds | First action |
| --- | --- | --- | --- |
| `ForetokenMetricsTargetDown` | Frontend or model | A metrics endpoint cannot be scraped for 1 minute; no configurable threshold. | Check the error in Prometheus Targets, then the affected Pod and network access to its metrics endpoint. |
| `ForetokenFrontendHTTPResponseStart5xxRatioHigh` | Frontend | HTTP response-start 5xx exceeds 5% for 2 minutes, with at least 0.1 response starts/s over a 5-minute window; no configurable threshold. | Inspect status-code trends and frontend logs, then admission rejections/timeouts, model availability, and backend errors. |
| `ForetokenAdmissionCapacityRejectionRatioHigh` | Frontend | Capacity rejection fraction exceeds `admission.capacityRejectionRatio` (0–1), with completed-call rate ≥ `admission.minResultRate` (calls/s, positive). | Compare Pod dispatch results, shared waiting occupancy, caller limits, and backend load before adjusting limits. |
| `ForetokenAdmissionTimeoutRatioHigh` | Frontend | Combined `queue_timeout` and `deadline_exceeded` fraction exceeds `admission.timeoutRatio` (0–1), with completed-call rate ≥ `admission.minResultRate` (calls/s, positive). | Compare occupancy and waiting time with `queueTimeout` and the request timeout; distinguish queue expiry from request-budget expiry before admission. |
| `ForetokenAdmissionAdmittedQueueP95High` | Frontend | Waiting-time p95 exceeds `admission.admittedQueueP95Seconds` (seconds, positive), with sampled-call rate ≥ `admission.minQueuedAdmissionRate` (calls/s, positive). | Compare waiting time with shared waiting occupancy and limits; inspect timeout results for requests leaving before backend acceptance. |
| `ForetokenAdmissionTelemetryMissing` | Frontend | Scraping succeeds but required admission metrics are missing or model inventory is inconsistent for 5 minutes; always evaluated per Pod. | Check the Admission replica table, Pod runtime versions, and monitoring configuration. |
| `ForetokenNVIDIAGPUTemperatureHigh` | Model | NVIDIA GPU temperature ≥ `nvidiaTemperatureCelsius` (°C, default 85) for 2 minutes. | Check the notification's device temperature, cooling, and workload. |
| `ForetokenNVIDIAGPUPowerUsageHigh` | Model | NVIDIA GPU power ≥ `nvidiaPowerWatts` (watts, positive, required; no default) for 5 minutes. | Compare device power and workload with its intended operating envelope and the alert threshold. |

Waiting-time p95 measures HTTP calls from acquiring waiting ownership to first backend acceptance, including input preparation and submission. Unrestricted, CPU-only, and timed-out calls are excluded; `minQueuedAdmissionRate` applies to this same sample set.
