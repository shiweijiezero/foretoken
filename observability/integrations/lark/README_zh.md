<!--
SPDX-License-Identifier: Apache-2.0
SPDX-FileCopyrightText: Copyright contributors to the Foretoken project
-->

# Lark 告警通知

[English](README.md) | 简体中文

通过群自定义机器人和集群中的 Alertmanager，将 Foretoken 告警发送到 Lark 群。通知包含受影响资源、告警详情、时间和排障链接。

## 开始前

启用 [Foretoken 告警](../../README_zh.md#告警)，并取得目标 Lark 群的自定义机器人 webhook。已安装的 Prometheus Operator 和 Alertmanager 需要支持 `webhookConfigs.payload`。

CLI 管理的 Alertmanager 会选中 `foretoken-platform` 中的配置，并允许这些配置接收工作负载命名空间的告警。

## 接入机器人

在 [alertmanagerconfig.yaml](alertmanagerconfig.yaml) 中将 `$language` 设为 `zh`（默认）、`en` 或 `bilingual`。`$timezone` 默认使用 `Local`，跟随 Alertmanager 容器的时区；可改为 `Europe/Berlin` 等 IANA 时区名称。消息中的时间包含 UTC 偏移。

在仓库根目录执行。使用[已有监控栈](#使用已有监控栈)时，将 `ALERTMANAGER_NAMESPACE` 设为它的 Alertmanager 命名空间。将 webhook 占位符替换为机器人的 URL：

```bash
ALERTMANAGER_NAMESPACE=foretoken-platform

# 将 webhook 保存到 Secret。
kubectl create secret generic foretoken-lark-webhook \
  --namespace "$ALERTMANAGER_NAMESPACE" \
  --from-literal=url='<LARK_CUSTOM_BOT_WEBHOOK_URL>'

# 在同一命名空间添加通知接收器。
kubectl apply \
  --namespace "$ALERTMANAGER_NAMESPACE" \
  --filename observability/integrations/lark/alertmanagerconfig.yaml
```

所选服务告警触发或解除时，群里会收到通知。

## 使用已有监控栈

监控管理员需要通过 `alertmanagerConfigSelector` 选中 `foretoken-lark`，并允许它接收 Foretoken 工作负载命名空间的告警。接收器与 Alertmanager 在同一命名空间时，可将 `spec.alertmanagerConfigMatcherStrategy.type` 设为 `OnNamespaceExceptForAlertmanagerNamespace`，见 [Operator API 参考](https://prometheus-operator.dev/docs/api-reference/api/#monitoring.coreos.com/v1.AlertmanagerConfigMatcherStrategy)。

## 移除集成

```bash
kubectl delete alertmanagerconfig foretoken-lark \
  --namespace "$ALERTMANAGER_NAMESPACE"
kubectl delete secret foretoken-lark-webhook \
  --namespace "$ALERTMANAGER_NAMESPACE"
```
