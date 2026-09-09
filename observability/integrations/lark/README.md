<!--
SPDX-License-Identifier: Apache-2.0
SPDX-FileCopyrightText: Copyright contributors to the Foretoken project
-->

# Lark notifications

English | [简体中文](README_zh.md) | [Alerting](../../alerting.md)

This receiver sends Foretoken alerts from Alertmanager to a Lark custom bot. It supports Chinese, English, and bilingual messages, grouped by alert name and message language. Alertmanager owns retries, grouping, and resolved notifications; the receiver does not deploy another notification service.

## Before you start

Enable [Foretoken alerts](../../alerting.md). The receiver requires Alertmanager 0.32 or newer and a Prometheus Operator CRD supporting `webhookConfigs.payload`. The CLI-managed kube-prometheus-stack 88.5.2 provides compatible versions: Alertmanager 0.34.0 and Prometheus Operator 0.93.1.

Create a Lark custom bot in the destination group. This direct receiver does not calculate Lark request signatures; use a bot without signature verification, or a platform-managed signing connector. A required keyword such as `Foretoken` is present in each message.

## Connect CLI-managed Alertmanager

The example uses `foretoken-platform`. Save the bot URL in a local secret file outside the repository, then create the Kubernetes Secret:

```bash
kubectl create secret generic foretoken-lark-webhook \
  --namespace foretoken-platform \
  --from-file=url=/secure/path/lark-webhook-url

kubectl apply --kustomize examples/alerting/lark

kubectl patch alertmanager foretoken-prometheus-kube-alertmanager \
  --namespace foretoken-platform \
  --type merge \
  --patch-file examples/alerting/lark/alertmanager-patch.yaml
```

Replace `/secure/path/lark-webhook-url` with the file containing only the bot URL. Update an existing Secret through the platform's secret-management workflow.

The patch selects only receiver configurations labeled `inference.foretoken.io/alert-receiver=lark` in `foretoken-platform`. It lets that trusted receiver match Foretoken alerts from workload namespaces; without this setting, the Operator normally adds a receiver-namespace matcher that would exclude them. Apply the patch after installing or upgrading the managed monitoring release.

## Use a shared Alertmanager

Change the namespace in `examples/alerting/lark/kustomization.yaml` and create the Secret there. Apply that Kustomize directory, then have the Alertmanager owner include its label and namespace in the existing receiver selectors and allow the required workload namespaces. Do not replace a shared installation's selectors with the CLI-managed example patch.

The receiver matches `service=foretoken`. Existing platform routes decide whether the same alerts also reach other channels.

## Message language and delivery

Set `alerting.language` to `zh`, `en`, or `bilingual` in the [platform example](../../../examples/alerting/platform.yaml), then rerun `foretoken install --values` with that file. Each message lists affected targets, severity, trigger or recovery time, summary, and runbook link. Times use `Asia/Shanghai`. Different languages remain separate notification groups.

Inspect the installed configuration:

```bash
kubectl get alertmanagerconfig foretoken-lark \
  --namespace foretoken-platform --output yaml
```

Confirm that the Operator has accepted the configuration and Alertmanager has loaded it without errors. Send a controlled test alert through Alertmanager with `service=foretoken`, a workload `namespace`, and the desired `notification_language`. Confirm both firing and resolved messages in the destination group. Alertmanager records HTTP transport success; only checking the group confirms that Lark accepted the bot credentials and message.

## Remove the receiver

```bash
kubectl delete --kustomize examples/alerting/lark
kubectl delete secret foretoken-lark-webhook --namespace foretoken-platform
```

Remove the example selector settings from the Alertmanager configuration if no longer used. This does not disable Prometheus alert rules; use `alerting.enabled: false` for that.

## References

- [Alertmanager webhook configuration](https://prometheus.io/docs/alerting/latest/configuration/#webhook_config)
- [Prometheus notification templates](https://prometheus.io/docs/alerting/latest/notifications/)
- [Prometheus Operator AlertmanagerConfig API](https://prometheus-operator.dev/docs/api-reference/api/#monitoring.coreos.com/v1alpha1.AlertmanagerConfig)
