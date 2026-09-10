<!--
SPDX-License-Identifier: Apache-2.0
SPDX-FileCopyrightText: Copyright contributors to the Foretoken project
-->

# Observability example

English | [简体中文](README_zh.md)

Runs the [Quick Start](../quickstart/README.md) model service with metrics, the Grafana dashboard, and alerts. `alerts.yaml` holds the alert thresholds and notification language; metric collection and the dashboard need no configuration. From the repository root:

```bash
foretoken install --values examples/observability/alerts.yaml
foretoken deploy examples/quickstart
```

Send a few requests, then open Grafana and select **Foretoken System Overview**. Alert thresholds appear as dashed lines on the matching panels. The [observability guide](../../observability/README.md) explains how to find Grafana, reuse an existing monitoring stack, and read the alerts.
