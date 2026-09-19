<!--
SPDX-License-Identifier: Apache-2.0
SPDX-FileCopyrightText: Copyright contributors to the Foretoken project
-->

# Slack 告警通知

[English](README.md) | 简体中文

通过 Alertmanager，将 Foretoken 服务告警发送到 Slack 频道。

## 接入 Slack

安装 Foretoken 平台后，为目标频道[创建 Slack incoming webhook](https://docs.slack.dev/messaging/sending-messages-using-incoming-webhooks/)。

Webhook 保存在 Alertmanager 所在命名空间的 Secret 中。CLI 管理的监控栈使用 `foretoken-platform`；使用[已有监控栈](#使用已有监控栈)时，修改下方命名空间。将 webhook 占位符替换为 Slack 提供的 URL：

```bash
ALERTMANAGER_NAMESPACE=foretoken-platform
kubectl create secret generic foretoken-slack-webhook \
  --namespace "$ALERTMANAGER_NAMESPACE" \
  --from-literal=url='<SLACK_INCOMING_WEBHOOK_URL>'
```

在平台 values 文件（如 `platform-values.yaml`）中添加：

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

启用需要的[服务告警](../../README_zh.md#告警)。告警触发或解除时，频道会收到告警状态、受影响资源、英文说明和排障链接。

## 使用已有监控栈

在同一个 `observability.notifications` 下，将 `namespace` 设为存放 Secret 的 Alertmanager 命名空间。如果 Alertmanager 按标签选择接收器，再填写 `additionalLabels`。例如：

```yaml
namespace: monitoring
additionalLabels:
  team: inference
```

监控管理员需要通过 `alertmanagerConfigSelector` 选中这个接收器，并允许它接收 Foretoken 工作负载命名空间的告警。接收器与 Alertmanager 在同一命名空间时，可将 `spec.alertmanagerConfigMatcherStrategy.type` 设为 `OnNamespaceExceptForAlertmanagerNamespace`，见 [Operator API 参考](https://prometheus-operator.dev/docs/api-reference/api/#monitoring.coreos.com/v1.AlertmanagerConfigMatcherStrategy)。

## 关闭 Slack 通知

将 `observability.notifications.slack.webhookSecret.name` 设为 `""`，再次执行安装命令，即可删除 Slack 接收器，其他通知渠道保持不变。Secret 不再使用时单独删除：

```bash
kubectl delete secret foretoken-slack-webhook --namespace "$ALERTMANAGER_NAMESPACE"
```

`foretoken uninstall` 也会删除接收器，并保留 Secret。
