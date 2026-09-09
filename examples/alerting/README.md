<!--
SPDX-License-Identifier: Apache-2.0
SPDX-FileCopyrightText: Copyright contributors to the Foretoken project
-->

# Alerting example

English | [简体中文](README_zh.md)

Enable Foretoken alert rules independently of collection. From the repository root:

```bash
foretoken install --values examples/alerting/platform.yaml
```

Keep existing platform values in the same command. Edit this example to choose thresholds or `alerting.language`; see the [alerting guide](../../observability/alerting.md). Setting `alerting.enabled: false` and repeating installation removes alert rules without removing metrics or the Dashboard.

The `lark/` Kustomize overlay installs an optional receiver into an existing Alertmanager. Create its webhook Secret first, then follow the [Lark guide](../../observability/integrations/lark/README.md), including receiver selection and firing/resolved verification.
