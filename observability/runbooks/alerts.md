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

| Rule | Service | Trigger |
| --- | --- | --- |
| [ForetokenMetricsTargetDown](#foretokenmetricstargetdown) | Frontend or model | A metrics endpoint cannot be scraped for 1 minute. |
| [ForetokenFrontendHTTPResponseStart5xxRatioHigh](#foretokenfrontendhttpresponsestart5xxratiohigh) | Frontend | HTTP response-start 5xx exceeds 5% for 2 minutes, with at least 0.1 responses/s over a 5-minute window. |
| [ForetokenAdmissionCapacityRejectionRatioHigh](#foretokenadmissioncapacityrejectionratiohigh) | Frontend | Admission capacity rejections exceed the configured fraction. |
| [ForetokenAdmissionTimeoutRatioHigh](#foretokenadmissiontimeoutratiohigh) | Frontend | Admission timeouts exceed the configured fraction. |
| [ForetokenAdmissionAdmittedQueueP95High](#foretokenadmissionadmittedqueuep95high) | Frontend | Queue-wait p95 for requests that queued and were admitted exceeds the configured duration. |
| [ForetokenAdmissionTelemetryMissing](#foretokenadmissiontelemetrymissing) | Frontend | Scraping succeeds but required admission metrics are missing for 5 minutes. |
| [ForetokenNVIDIAGPUTemperatureHigh](#foretokennvidiagputemperaturehigh) | Model | NVIDIA GPU temperature reaches the threshold for 2 minutes; default 85°C. |
| [ForetokenNVIDIAGPUPowerUsageHigh](#foretokennvidiagpupowerusagehigh) | Model | NVIDIA GPU power reaches the configured threshold for 5 minutes. |

Admission thresholds go under `spec.observability.alerts.thresholds.admission`. The three ratio/latency rules require explicit thresholds and positive minimum rates. Optional `scope` defaults to `service` and can be `pod`; `window` defaults to `1m` and `for` to `5m`. Durations accept whole seconds, minutes, or hours. These settings control the aggregation, calculation window, and time the condition must persist before the alert fires.

## Investigate an alert

Open Foretoken System Overview in [Grafana](../README.md#view-dashboards) and select the notification's namespace and frontend or model.

### ForetokenMetricsTargetDown

Check the scrape error in Prometheus Targets, then inspect the affected Pod and network access to its metrics endpoint.

### ForetokenFrontendHTTPResponseStart5xxRatioHigh

Inspect frontend status-code trends and logs. Use the Admission section to identify capacity rejection or timeout responses, then check model availability and backend errors.

### ForetokenAdmissionCapacityRejectionRatioHigh

Set `capacityRejectionRatio` from 0 to 1 and `minResultRate` in completed calls/s under the admission thresholds. The ratio counts capacity rejections among completed calls, separately for each admission stage.

Compare each frontend Pod's traffic, occupancy, and configured limits. `intake` identifies HTTP residency limits; `work` identifies work admission. Check backend load before adjusting frontend limits.

### ForetokenAdmissionTimeoutRatioHigh

Set `timeoutRatio` from 0 to 1 and `minResultRate` in completed calls/s under the admission thresholds. The ratio counts `queue_timeout` and `deadline_exceeded` among completed work-admission calls.

Compare queue occupancy and waiting time with `queueTimeout` and the request timeout. The result breakdown separates queue expiry from the request budget expiring before admission.

### ForetokenAdmissionAdmittedQueueP95High

Set positive `admittedQueueP95Seconds` in seconds and `minQueuedAdmissionRate` in calls/s under the admission thresholds. This rule measures only requests that actually queued and were admitted, not immediate admissions or timed-out requests.

Inspect the admitted-wait curve alongside queue occupancy and model capacity. Use timeout results to see whether requests are also leaving the queue without admission.

### ForetokenAdmissionTelemetryMissing

Inspect the Admission replica table for incomplete reporting and compare Pod runtime versions. Check monitoring configuration if the issue persists after an upgrade completes.

### ForetokenNVIDIAGPUTemperatureHigh

To change the default threshold, set `spec.observability.alerts.thresholds.nvidiaTemperatureCelsius` in °C.

Check temperature, cooling, and workload on the device named in the notification.

### ForetokenNVIDIAGPUPowerUsageHigh

Set a positive `spec.observability.alerts.thresholds.nvidiaPowerWatts` threshold in watts; this rule has no default threshold.

Compare the device's power draw and workload with its intended operating envelope and configured alert threshold.
