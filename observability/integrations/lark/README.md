<!--
SPDX-License-Identifier: Apache-2.0
SPDX-FileCopyrightText: Copyright contributors to the Foretoken project
-->

# Lark alert notifications

English | [简体中文](README_zh.md)

Send Foretoken alerts to a Lark group through its custom bot and the cluster's Alertmanager. Notifications include affected resources, alert details, timestamps, and runbook links.

## Before you start

Enable [Foretoken alerts](../../README.md#alerts) and obtain a custom bot webhook from the destination Lark group. The installed Prometheus Operator and Alertmanager must support `webhookConfigs.payload`.

The CLI-managed Alertmanager selects configurations in `foretoken-platform` and allows them to receive alerts from workload namespaces.

## Connect the bot

In [alertmanagerconfig.yaml](alertmanagerconfig.yaml), set `$language` to `zh` (the default), `en`, or `bilingual`. `$timezone` defaults to `Local`, using the Alertmanager container's time zone; an IANA name such as `Europe/Berlin` overrides it. Messages include the UTC offset.

Run from the repository root. For an [existing monitoring stack](#use-an-existing-monitoring-stack), set `ALERTMANAGER_NAMESPACE` to its Alertmanager namespace. Replace the webhook placeholder with the bot's URL:

```bash
ALERTMANAGER_NAMESPACE=foretoken-platform

# Store the webhook in a Secret.
kubectl create secret generic foretoken-lark-webhook \
  --namespace "$ALERTMANAGER_NAMESPACE" \
  --from-literal=url='<LARK_CUSTOM_BOT_WEBHOOK_URL>'

# Add the notification receiver in the same namespace.
kubectl apply \
  --namespace "$ALERTMANAGER_NAMESPACE" \
  --filename observability/integrations/lark/alertmanagerconfig.yaml
```

The group receives a notification when a selected service alert fires or resolves.

## Use an existing monitoring stack

The monitoring administrator must select `foretoken-lark` through `alertmanagerConfigSelector` and allow alerts from Foretoken workload namespaces. For a receiver in Alertmanager's own namespace, `spec.alertmanagerConfigMatcherStrategy.type: OnNamespaceExceptForAlertmanagerNamespace` provides this behavior. See the [Operator API reference](https://prometheus-operator.dev/docs/api-reference/api/#monitoring.coreos.com/v1.AlertmanagerConfigMatcherStrategy).

## Remove the integration

```bash
kubectl delete alertmanagerconfig foretoken-lark \
  --namespace "$ALERTMANAGER_NAMESPACE"
kubectl delete secret foretoken-lark-webhook \
  --namespace "$ALERTMANAGER_NAMESPACE"
```
