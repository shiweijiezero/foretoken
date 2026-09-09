<!--
SPDX-License-Identifier: Apache-2.0
SPDX-FileCopyrightText: Copyright contributors to the Foretoken project
-->

# Alerting and Lark notifications

[Observability](README.md) | [简体中文](alerting_zh.md)

Foretoken publishes metrics and recording rules, but it does not install alert rules or manage notification receivers. Alert thresholds, Alertmanager routing, and Lark delivery belong to the platform's monitoring configuration.

## Define alert rules

Base alerts on the stable recording rules documented in [Observability](README.md). For example, a platform-owned Prometheus rule group can alert when no Frontend target is reporting:

```yaml
groups:
  - name: foretoken.platform
    rules:
      - alert: ForetokenFrontendUnavailable
        expr: foretoken:frontend_up:sum == 0
        for: 10m
        labels:
          severity: warning
          team: inference
        annotations:
          summary: No Foretoken Frontend target is reporting
          description: The selected Foretoken Frontend has reported no healthy target for 10 minutes.
```

The `PrometheusRule` metadata, selector labels, evaluation interval, and ownership remain specific to the Prometheus Operator installation. Add the rule through that platform's configuration repository or GitOps workflow rather than through the Foretoken Helm chart. Choose thresholds and `for` durations from the service's operational objectives; the example is a starting point, not a Foretoken default.

A response can start with `2xx` and fail later while streaming. Do not treat `foretoken:frontend_http_response_start_5xx_ratio:rate5m` as an inference-success SLO. Pair response-start signals with service and model-serving signals when defining an incident condition.

## Deliver notifications through Lark

Foretoken has no built-in Lark receiver. To send alerts to Lark:

1. Configure a Lark-compatible webhook or notification connector in the platform's Alertmanager integration.
2. Store webhook URLs, signing secrets, and other credentials in the platform's Secret management. Do not put them in Foretoken values, examples, or Git.
3. Route the platform-owned alert labels, such as `team` and `severity`, to that receiver.
4. Trigger a controlled test alert through the platform's normal Alertmanager workflow and confirm delivery, deduplication, and recovery notifications in Lark.

The exact receiver and Secret schema depend on the Alertmanager integration used by the cluster. Keep that integration outside Foretoken so `foretoken uninstall` does not remove platform-owned notification policy.

## Ownership and cleanup

The platform team owns alert expressions, thresholds, silences, escalation, Alertmanager receivers, and Lark access. Foretoken owns the metrics and recording rules installed with its platform release. Removing Foretoken services and running `foretoken uninstall` does not delete platform-owned alert rules or Lark integrations.
