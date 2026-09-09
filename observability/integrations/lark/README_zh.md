<!--
SPDX-License-Identifier: Apache-2.0
SPDX-FileCopyrightText: Copyright contributors to the Foretoken project
-->

# Lark 通知

[English](README.md) | 简体中文 | [告警](../../alerting_zh.md)

该接收器把 Alertmanager 中的 Foretoken 告警发送到 Lark 自定义机器人，支持中文、英文和双语消息，按告警名称及消息语言分组。重试、分组和解除通知由 Alertmanager 负责，不会额外部署通知服务。

## 开始前

先[启用 Foretoken 告警](../../alerting_zh.md)。接收器需要 Alertmanager 0.32 或更新版本，以及支持 `webhookConfigs.payload` 的 Prometheus Operator CRD。CLI 管理的 kube-prometheus-stack 88.5.2 包含兼容版本：Alertmanager 0.34.0 和 Prometheus Operator 0.93.1。

在目标群创建 Lark 自定义机器人。此直连接收器不计算 Lark 请求签名；应使用未开启签名校验的机器人，或复用平台已有的签名连接器。每条消息包含 `Foretoken`，可将其设置为机器人的必需关键词。

## 接入 CLI 管理的 Alertmanager

示例使用 `foretoken-platform` 命名空间。将机器人 URL 保存到仓库之外的本地凭据文件，然后创建 Kubernetes Secret：

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

将 `/secure/path/lark-webhook-url` 替换为只包含机器人 URL 的文件路径。若 Secret 已存在，通过平台已有的 Secret 管理流程更新。

补丁只选择 `foretoken-platform` 中带有 `inference.foretoken.io/alert-receiver=lark` 标签的接收器配置，并允许这个可信接收器匹配 workload namespace 中的 Foretoken 告警。否则 Operator 默认追加的接收器 namespace 条件会排除这些告警。安装或升级受管监控 release 后，再应用该补丁。

## 接入共享 Alertmanager

修改 `examples/alerting/lark/kustomization.yaml` 的 namespace，在相同命名空间中创建 Secret，再应用该 Kustomize 目录。由 Alertmanager 管理者把接收器的标签和命名空间纳入已有选择器，并允许所需的 workload namespace。不要用受管实例的示例补丁覆盖共享平台的选择器。

接收器匹配 `service=foretoken`。同一告警是否同时发送到其他渠道，由平台现有路由决定。

## 消息语言与投递验证

在[平台配置示例](../../../examples/alerting/platform.yaml)中将 `alerting.language` 设为 `zh`、`en` 或 `bilingual`，再通过 `foretoken install --values` 应用该文件。每条消息列出受影响目标、级别、触发或解除时间、摘要和排障链接。时间采用 `Asia/Shanghai`；不同语言的告警分别分组。

检查已安装的配置：

```bash
kubectl get alertmanagerconfig foretoken-lark \
  --namespace foretoken-platform --output yaml
```

确认 Operator 接受了配置，且 Alertmanager 加载时没有报错。通过 Alertmanager 发送携带 `service=foretoken`、workload `namespace` 和目标 `notification_language` 的受控测试告警，分别确认目标群收到了触发与解除消息。Alertmanager 只记录 HTTP 传输结果；实际查看目标群，才能确认 Lark 接受了机器人凭据和消息。

## 移除接收器

```bash
kubectl delete --kustomize examples/alerting/lark
kubectl delete secret foretoken-lark-webhook --namespace foretoken-platform
```

不再使用时，也从 Alertmanager 配置中移除示例的选择器设置。这不会关闭 Prometheus 告警规则；关闭规则请设置 `alerting.enabled: false`。

## 参考

- [Alertmanager webhook 配置](https://prometheus.io/docs/alerting/latest/configuration/#webhook_config)
- [Prometheus 通知模板](https://prometheus.io/docs/alerting/latest/notifications/)
- [Prometheus Operator AlertmanagerConfig API](https://prometheus-operator.dev/docs/api-reference/api/#monitoring.coreos.com/v1alpha1.AlertmanagerConfig)
