<!--
SPDX-License-Identifier: Apache-2.0
SPDX-FileCopyrightText: Copyright contributors to the Foretoken project
-->

# Foretoken alert reference

[简体中文](alerts_zh.md) | English

Select rules in the owning service's `spec.observability.alerts.rules`. An empty list disables alerts. Prometheus must discover the workload namespace and the selected `PrometheusRule`; metrics remain available when no alerts are selected.

| Alert | Service | Trigger | Persistence | Required threshold or scope |
| --- | --- | --- | --- | --- |
| `ForetokenMetricsTargetDown` | `FrontendService` or `ModelService` | A discovered `/metrics` endpoint cannot be scraped | 1 minute | The selected service's metrics endpoint |
| `ForetokenFrontendHTTPResponseStart5xxRatioHigh` | `FrontendService` | More than 5% of response starts are 5xx while traffic is at least 0.1 response/s | 2 minutes | The selected FrontendService |
| `ForetokenNVIDIAGPUTemperatureHigh` | `ModelService` | An attributed NVIDIA GPU reaches the configured temperature threshold | 2 minutes | `nvidiaTemperatureCelsius` (default 85°C); ModelGroup scope |
| `ForetokenNVIDIAGPUPowerUsageHigh` | `ModelService` | An attributed NVIDIA GPU reaches the configured power threshold | 5 minutes | Select the rule and set positive `nvidiaPowerWatts`; ModelGroup scope |

GPU temperature and power rules use recording series attributed to the selected ModelService's ModelGroups. The dashboard also provides GPU utilization and memory-occupancy metrics.

`ForetokenMetricsTargetDown` resolves when scraping resumes or the metrics endpoint leaves service discovery.

For delivery, configure a [Lark](../integrations/lark/README.md), [Slack](../integrations/slack/README.md), or [DingTalk](../integrations/dingtalk/README.md) receiver.
