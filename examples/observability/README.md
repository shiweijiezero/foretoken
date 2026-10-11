<!--
SPDX-License-Identifier: Apache-2.0
SPDX-FileCopyrightText: Copyright contributors to the Foretoken project
-->

# Enable Service Alerts

English | [简体中文](README_zh.md)

Deploy the Quick Start model and frontend with service alerts. The example uses the same resources, namespace, and data directory as the [Quick Start](../quickstart/README.md).

## Select and deploy alerts

Install the platform following the repository [Quick Start](../../README.md#quick-start). In [`observability.yaml`](observability.yaml), replace `rules: []` in the frontend or model service with the rules you want. For a metrics-endpoint alert, use:

```yaml
spec:
  observability:
    alerts:
      rules:
        - ForetokenMetricsTargetDown
```

Both lists are empty by default. Select frontend and model rules independently; the [alert reference](../../observability/runbooks/alerts.md) lists their trigger conditions and required thresholds.

From the repository root:

```bash
foretoken deploy examples/observability --timeout 20m
```

Send a request using the [Quick Start request example](../quickstart/README.md). Open Foretoken System Overview in [Grafana](../../observability/README.md#view-dashboards), select `foretoken-demo`, and check the selected rules in Prometheus Alerts. Configure a [notification receiver](../../observability/README.md#alerts) to receive firing and resolved messages.

## Disable alerts or remove the service

Remove a rule from `observability.yaml` and redeploy the directory to disable it. To remove the model and frontend deployment:

```bash
foretoken delete examples/observability
```

Shared monitoring remains installed.
