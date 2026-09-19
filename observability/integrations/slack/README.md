<!--
SPDX-License-Identifier: Apache-2.0
SPDX-FileCopyrightText: Copyright contributors to the Foretoken project
-->

# Slack alert notifications

English | [简体中文](README_zh.md)

With Foretoken installed, [create an incoming webhook](https://docs.slack.dev/messaging/sending-messages-using-incoming-webhooks/) for the destination Slack channel. Store it in Alertmanager's namespace (`foretoken-platform` for CLI-managed monitoring):

```bash
ALERTMANAGER_NAMESPACE=foretoken-platform
kubectl create secret generic foretoken-slack-webhook \
  --namespace "$ALERTMANAGER_NAMESPACE" \
  --from-literal=url='<SLACK_INCOMING_WEBHOOK_URL>'
```

Add to `platform-values.yaml`:

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

Enable the [service alerts](../../README.md#alerts) you need. Firing and resolved notifications include affected resources, English descriptions and runbook links.

## Existing monitoring stacks

Set `observability.notifications.namespace` to your Alertmanager namespace, matching the Secret above. Set `additionalLabels` under the same mapping if its `alertmanagerConfigSelector` requires labels.

The monitoring administrator selects the receiver and permits workload alerts. With the receiver in Alertmanager's own namespace, use `spec.alertmanagerConfigMatcherStrategy.type: OnNamespaceExceptForAlertmanagerNamespace`; see the [Operator API](https://prometheus-operator.dev/docs/api-reference/api/#monitoring.coreos.com/v1.AlertmanagerConfigMatcherStrategy).

## Disconnect

Set `observability.notifications.slack.webhookSecret.name` to `""` and repeat the install command to remove the receiver. `foretoken uninstall` also removes it. Both leave the Secret in place; delete it when no longer needed:

```bash
kubectl delete secret foretoken-slack-webhook --namespace "$ALERTMANAGER_NAMESPACE"
```
