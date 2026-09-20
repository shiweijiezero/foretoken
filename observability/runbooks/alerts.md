<!--
SPDX-License-Identifier: Apache-2.0
SPDX-FileCopyrightText: Copyright contributors to the Foretoken project
-->

# Foretoken alert reference

[简体中文](alerts_zh.md) | English

Select rules in the owning service's `spec.observability.alerts.rules`. An empty list disables alerts. Prometheus must discover the workload namespace and the selected `PrometheusRule`; metrics remain available when no alerts are selected.

| Alert | Service | Trigger | Persistence | Required threshold or scope |
| --- | --- | --- | --- | --- |
| `ForetokenMetricsTargetDown` | `FrontendService` or `ModelService` | A discovered `/metrics` target cannot be scraped | 1 minute | The selected service's target |
| `ForetokenFrontendHTTPResponseStart5xxRatioHigh` | `FrontendService` | More than 5% of response starts are 5xx while traffic is at least 0.1 response/s | 2 minutes | The selected FrontendService |
| `ForetokenNVIDIAGPUTemperatureHigh` | `ModelService` | An attributed NVIDIA GPU reaches the configured temperature threshold | 2 minutes | `nvidiaTemperatureCelsius` (default 85°C); ModelGroup scope |
| `ForetokenNVIDIAGPUPowerUsageHigh` | `ModelService` | An attributed NVIDIA GPU reaches the configured power threshold | 5 minutes | Select the rule and set positive `nvidiaPowerWatts`; ModelGroup scope |

GPU temperature and power rules use only recording series attributed to the selected ModelService's ModelGroups. GPU utilization and memory-occupancy alerts are not provided, but their metrics remain available in the dashboard.

`ForetokenMetricsTargetDown` resolves when scraping resumes or the target leaves service discovery.

For delivery, configure a [Lark](../integrations/lark/README.md) or [Slack](../integrations/slack/README.md) receiver.
