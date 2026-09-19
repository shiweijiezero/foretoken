<!--
SPDX-License-Identifier: Apache-2.0
SPDX-FileCopyrightText: Copyright contributors to the Foretoken project
-->

# Slack alert notifications

English | [简体中文](README_zh.md)

Send Foretoken service alerts to a Slack channel through Alertmanager.

## Connect Slack

With the Foretoken platform installed, [create a Slack incoming webhook](https://docs.slack.dev/messaging/sending-messages-using-incoming-webhooks/) for the destination channel.

Store the webhook in Alertmanager's namespace. The CLI-managed stack uses `foretoken-platform`; for an [existing monitoring stack](#use-an-existing-monitoring-stack), change the namespace below. Replace the webhook placeholder with the URL from Slack:

```bash
ALERTMANAGER_NAMESPACE=foretoken-platform
kubectl create secret generic foretoken-slack-webhook \
  --namespace "$ALERTMANAGER_NAMESPACE" \
  --from-literal=url='<SLACK_INCOMING_WEBHOOK_URL>'
```

Add to your platform values file, such as `platform-values.yaml`:

```yaml
observability:
  notifications:
    slack:
      webhookSecret:
        name: foretoken-slack-webhook
```

Apply using the platform's original installation mode:

```bash
foretoken install --values platform-values.yaml
# For a source-installed platform, run from the repository root:
# foretoken install -e . --values platform-values.yaml
```

Enable the [service alerts](../../README.md#alerts) you need. When an alert fires or resolves, the channel receives its status, affected resources, English description, and runbook link.

## Use an existing monitoring stack

Under the same `observability.notifications` mapping, set `namespace` to the Alertmanager namespace used for the Secret. Add `additionalLabels` if Alertmanager requires labels to select the receiver. For example:

```yaml
namespace: monitoring
additionalLabels:
  team: inference
```

The monitoring administrator must select this receiver through `alertmanagerConfigSelector` and allow alerts from Foretoken workload namespaces. For a receiver in Alertmanager's own namespace, set `spec.alertmanagerConfigMatcherStrategy.type` to `OnNamespaceExceptForAlertmanagerNamespace`. See the [Operator API reference](https://prometheus-operator.dev/docs/api-reference/api/#monitoring.coreos.com/v1.AlertmanagerConfigMatcherStrategy).

## Disconnect Slack

Set `observability.notifications.slack.webhookSecret.name` to `""` and repeat the install command. This removes the Slack receiver without affecting other notification channels. Delete the Secret when it is no longer needed:

```bash
kubectl delete secret foretoken-slack-webhook --namespace "$ALERTMANAGER_NAMESPACE"
```

`foretoken uninstall` also removes the receiver and leaves the Secret in place.
