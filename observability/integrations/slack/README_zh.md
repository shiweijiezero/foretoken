<!--
SPDX-License-Identifier: Apache-2.0
SPDX-FileCopyrightText: Copyright contributors to the Foretoken project
-->

# Slack 告警通知

[English](README.md) | 简体中文

安装 Foretoken 后，为目标 Slack 频道[创建通知接收地址（Webhook）](https://docs.slack.dev/messaging/sending-messages-using-incoming-webhooks/)，保存到 Alertmanager 所在命名空间。CLI 管理的监控使用 `foretoken-platform`：

```bash
ALERTMANAGER_NAMESPACE=foretoken-platform
kubectl create secret generic foretoken-slack-webhook \
  --namespace "$ALERTMANAGER_NAMESPACE" \
  --from-literal=url='<SLACK_INCOMING_WEBHOOK_URL>'
```

在 `platform-values.yaml` 中添加：

```yaml
observability:
  notifications:
    slack:
      webhookSecret:
        name: foretoken-slack-webhook
```

沿用平台原来的安装方式应用配置：

```bash
foretoken install --values platform-values.yaml
# 源码安装的平台，在仓库根目录执行：
# foretoken install -e . --values platform-values.yaml
```

启用需要的[服务告警](../../README_zh.md#告警)。触发和解除通知包含受影响资源、英文说明和排障链接。

## 已有监控栈

将 `observability.notifications.namespace` 设为 Alertmanager 所在命名空间，与上面的 Secret 一致。如果其 `alertmanagerConfigSelector` 要求标签，在同一配置下填写 `additionalLabels`。

由监控管理员选中接收器并允许工作负载告警。接收器与 Alertmanager 同命名空间时，使用 `spec.alertmanagerConfigMatcherStrategy.type: OnNamespaceExceptForAlertmanagerNamespace`，见 [Operator API](https://prometheus-operator.dev/docs/api-reference/api/#monitoring.coreos.com/v1.AlertmanagerConfigMatcherStrategy)。

## 关闭通知

将 `observability.notifications.slack.webhookSecret.name` 设为 `""`，再次执行安装命令即可删除接收器；`foretoken uninstall` 也会删除它。两种方式都保留 Secret，不再使用时单独删除：

```bash
kubectl delete secret foretoken-slack-webhook --namespace "$ALERTMANAGER_NAMESPACE"
```
