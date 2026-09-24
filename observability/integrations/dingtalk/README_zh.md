<!--
SPDX-License-Identifier: Apache-2.0
SPDX-FileCopyrightText: Copyright contributors to the Foretoken project
-->

# 钉钉告警通知

[English](README.md) | 简体中文

通过群自定义机器人接收 Foretoken 服务告警。

## 接入机器人

使用 CLI 管理的监控时，先按原安装方式更新平台：

```bash
foretoken install
# 源码安装的平台，在仓库根目录执行：
# foretoken install -e .
```

按[钉钉文档](https://open.dingtalk.com/document/robots/custom-robot-access)创建群自定义机器人，安全设置选择自定义关键词 `Foretoken`，关闭加签。取得消息接收地址（Webhook）后，在仓库根目录执行以下命令，并替换 URL 占位符。使用已有监控栈时，改为其 Alertmanager 所在命名空间：

```bash
ALERTMANAGER_NAMESPACE=foretoken-platform

# 保存 webhook。
kubectl create secret generic foretoken-dingtalk-webhook \
  --namespace "$ALERTMANAGER_NAMESPACE" \
  --from-literal=url='<DINGTALK_CUSTOM_BOT_WEBHOOK_URL>'

# 在同一命名空间应用接收器。
kubectl apply --namespace "$ALERTMANAGER_NAMESPACE" \
  --filename observability/integrations/dingtalk/alertmanagerconfig.yaml
```

启用需要的[服务告警](../../README_zh.md#告警)，群里即可收到触发和解除通知，默认使用中文。

需要调整语言或时区时，修改[接收器配置](alertmanagerconfig.yaml)后重新执行 `kubectl apply`。`$language` 可设为 `zh`、`en` 或 `bilingual`；`$timezone` 可用 `Local`（容器时区）或 `Europe/Berlin` 等 IANA 时区名。

## 使用已有监控栈

Prometheus Operator 和 Alertmanager 需支持 `webhookConfigs.payload`。由监控管理员通过 `alertmanagerConfigSelector` 选中 `foretoken-dingtalk` 并允许工作负载告警。接收器与 Alertmanager 同命名空间时，使用 `spec.alertmanagerConfigMatcherStrategy.type: OnNamespaceExceptForAlertmanagerNamespace`，见 [Operator API](https://prometheus-operator.dev/docs/api-reference/api/#monitoring.coreos.com/v1.AlertmanagerConfigMatcherStrategy)。

## 移除集成

```bash
kubectl delete alertmanagerconfig foretoken-dingtalk --namespace "$ALERTMANAGER_NAMESPACE"
kubectl delete secret foretoken-dingtalk-webhook --namespace "$ALERTMANAGER_NAMESPACE"
```
