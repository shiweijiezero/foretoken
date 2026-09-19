<!--
SPDX-License-Identifier: Apache-2.0
SPDX-FileCopyrightText: Copyright contributors to the Foretoken project
-->

# Lark alert notifications

English | [简体中文](README_zh.md)

Send Foretoken service alerts to a Lark group through its custom bot.

## Connect the bot

For CLI-managed monitoring, update the platform using its original installation mode:

```bash
foretoken install
# For a source-installed platform, run from the repository root:
# foretoken install -e .
```

Obtain the group's bot webhook. Run from the repository root, replacing the URL placeholder. For an existing monitoring stack, use its Alertmanager namespace:

```bash
ALERTMANAGER_NAMESPACE=foretoken-platform

# Store the webhook.
kubectl create secret generic foretoken-lark-webhook \
  --namespace "$ALERTMANAGER_NAMESPACE" \
  --from-literal=url='<LARK_CUSTOM_BOT_WEBHOOK_URL>'

# Apply the receiver in the same namespace.
kubectl apply --namespace "$ALERTMANAGER_NAMESPACE" \
  --filename observability/integrations/lark/alertmanagerconfig.yaml
```

Enable the [service alerts](../../README.md#alerts) you need. The group receives firing and resolved notifications in Chinese by default.

For a different language or time zone, edit [the receiver configuration](alertmanagerconfig.yaml) and reapply it. Set `$language` to `zh`, `en` or `bilingual`, and `$timezone` to `Local` (the container's time zone) or an IANA name such as `Europe/Berlin`.

## Use an existing monitoring stack

Prometheus Operator and Alertmanager must support `webhookConfigs.payload`. The monitoring administrator selects `foretoken-lark` through `alertmanagerConfigSelector` and permits workload alerts. With the receiver in Alertmanager's own namespace, use `spec.alertmanagerConfigMatcherStrategy.type: OnNamespaceExceptForAlertmanagerNamespace`; see the [Operator API](https://prometheus-operator.dev/docs/api-reference/api/#monitoring.coreos.com/v1.AlertmanagerConfigMatcherStrategy).

## Remove the integration

```bash
kubectl delete alertmanagerconfig foretoken-lark --namespace "$ALERTMANAGER_NAMESPACE"
kubectl delete secret foretoken-lark-webhook --namespace "$ALERTMANAGER_NAMESPACE"
```
