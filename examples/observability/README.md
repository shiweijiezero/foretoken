<!--
SPDX-License-Identifier: Apache-2.0
SPDX-FileCopyrightText: Copyright contributors to the Foretoken project
-->

# Metrics and Dashboard

English | [简体中文](README_zh.md)

Use `platform.yaml` to configure Foretoken metric collection and its Grafana Dashboard without editing the Chart. From the repository root:

```bash
foretoken install --values examples/observability/platform.yaml
foretoken deploy examples/quickstart
```

Existing model deployments do not need to be redeployed. Keep any existing hardware or image values in the installation command. To use the current checkout's images and Chart, add `-e .` to `foretoken install`.

Open Prometheus and Grafana using the [observability guide](../../observability/README.md). Alerting is configured separately in [`examples/alerting`](../alerting/README.md).
