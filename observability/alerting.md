<!--
SPDX-License-Identifier: Apache-2.0
SPDX-FileCopyrightText: Copyright contributors to the Foretoken project
-->

# Alerting

English | [简体中文](alerting_zh.md)

Foretoken provides alert rules for unavailable metrics targets, Frontend HTTP errors, persistent scheduler queues, KV-cache pressure, and accelerator utilization, memory, temperature, and power. Alerting is enabled separately from [metric collection](README.md). Prometheus evaluates the rules; Alertmanager groups, routes, and delivers notifications through the platform's chosen channels.

## Enable alerts

Start with the maintained configuration in [`examples/alerting/platform.yaml`](../examples/alerting/platform.yaml):

```yaml
observability:
  mode: enabled
alerting:
  enabled: true
```

Install or update the platform with this file. Keep any existing platform values in the same command:

```bash
foretoken install --values examples/alerting/platform.yaml
```

This adds the `foretoken.alerting` rule group alongside collection and recording rules. It does not change model deployments or install a notification receiver. To build and install the current checkout instead of published artifacts, use `foretoken install -e . --values examples/alerting/platform.yaml`.

## Check rule evaluation

```bash
kubectl get prometheusrule foretoken-control-plane-alerting-rules \
  --namespace foretoken-platform
```

Open Prometheus through the [collection guide](README.md#verify-collection). Under **Rules**, confirm that `foretoken.alerting` is loaded without evaluation errors. Under **Alerts**, an unhealthy signal first becomes pending and then firing after its persistence window. It resolves when the expression is no longer true. The [runbooks](runbooks/alerts.md) describe each condition and the corresponding investigation.

## Choose thresholds and message language

Edit the example YAML, not the Chart sources. For example:

```yaml
observability:
  mode: enabled
alerting:
  enabled: true
  language: en
  thresholds:
    acceleratorMemoryUsageRatio: 0.90
    nvidiaTemperatureCelsius: 80
```

The hardware options are `acceleratorUtilizationRatio`, `acceleratorMemoryUsageRatio`, `nvidiaTemperatureCelsius`, and `nvidiaPowerWatts`. Ratios use the range 0–1; temperature uses degrees Celsius and power uses watts. Unspecified values retain the platform defaults. Select thresholds from the device's limits and measured workload behavior.

`language` accepts `zh`, `en`, or `bilingual` and labels notifications for receivers that support those languages. Rules retain both English and Chinese annotations. Message language applies to an alert group, not to individual recipients.

## Notification channels

Use Alertmanager's existing receivers for email, Slack, generic webhooks, or other supported destinations. Foretoken alerts carry `service=foretoken` and `severity=warning`; configure receiver routes to match those labels.

The optional [Lark integration](integrations/lark/README.md) provides a ready-to-apply receiver, Secret reference, and message template. It uses the existing Alertmanager rather than creating a second notification service.

A successful rule evaluation does not verify delivery. For a new receiver, send a controlled firing and resolved alert carrying the same labels and workload namespace as real alerts, and confirm both messages at the destination.

## Disable alerts

Set `alerting.enabled: false` in the same configuration and rerun the installation command. This removes Foretoken alert rules while leaving collection, recording rules, and the Dashboard available. It does not remove platform-owned receivers, credentials, silences, or notification policy. `foretoken uninstall` removes the release's alert rules with the rest of its resources.

Configurations written before the independent alerting switch should move `observability.alerts.language` and `observability.alerts.thresholds` to `alerting`, and set `alerting.enabled: true` to keep alerts enabled.
