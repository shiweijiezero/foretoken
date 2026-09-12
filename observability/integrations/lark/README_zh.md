<!--
SPDX-License-Identifier: Apache-2.0
SPDX-FileCopyrightText: Copyright contributors to the Foretoken project
-->

# Lark 告警通知

[English](README.md) | 简体中文

这个可选集成把现有 Prometheus Alertmanager 中带有 `service=foretoken` 标签的告警发送给 Lark 自定义机器人。Foretoken 负责告警标签和消息字段；平台负责 Alertmanager 实例、接收群以及 webhook 凭据。

这里的清单是部署来源，不会创建第二套 Alertmanager。应用清单后，集群中会生成一个 `AlertmanagerConfig`。Webhook URL 必须保存在 Kubernetes Secret 中，不得写入仓库。

Foretoken 会为每条告警添加 `notification_language` 标签。在[可观测性示例](../../../examples/observability/observability.yaml)中将 `observability.alerts.language` 设为 `zh`、`en` 或 `bilingual`，通过 `foretoken install --values` 传入，Payload 就会选择中文、英文或双语消息。由于一条 Alertmanager 消息可能包含多条告警，这个选择作用于一次部署共享的消息，不能针对同一条消息中的不同接收人单独选择语言。

## 前提条件

- Prometheus Operator 会为目标 Alertmanager 选择 `AlertmanagerConfig`；
- 已安装的 CRD 和 Alertmanager 版本支持 `webhookConfigs.payload`；
- 已创建 Lark 自定义机器人 webhook；
- 平台的 namespace 匹配策略允许接收 Foretoken workload namespace 中的告警。

Prometheus Operator 通常会把 `AlertmanagerConfig` 路由限制在资源自身的 namespace。即使配置和 Alertmanager 一起放在 `monitoring`，带有其他 workload `namespace` 标签的告警仍可能无法匹配。Alertmanager 管理者必须检查 `alertmanagerConfigMatcherStrategy` 并明确决定跨 namespace 策略；本集成不会替平台修改它。

## 安装

选择 Alertmanager 所属 namespace，并在其中创建被引用的 Secret：

```bash
ALERTMANAGER_NAMESPACE=monitoring
kubectl create secret generic foretoken-lark-webhook \
  --namespace "$ALERTMANAGER_NAMESPACE" \
  --from-literal=url='<LARK_CUSTOM_BOT_WEBHOOK_URL>'
```

把 receiver 应用到相同 namespace：

```bash
kubectl apply \
  --namespace "$ALERTMANAGER_NAMESPACE" \
  --filename observability/integrations/lark/alertmanagerconfig.yaml
```

如果 Secret 已存在，应通过平台的 Secret 管理流程更新，不要把值提交到仓库。

## 消息契约

路由选择 Foretoken 告警规则统一添加的 `service=foretoken` 标签。Payload 按稳定的机器告警名称分组，根据语言显示告警名称（`alertname_zh`）、级别、字段标题、摘要和详情。资源名称和设备编号保持原样，方便定位目标；每个目标的触发和解除时间按 `Asia/Shanghai` 显示。

GPU 消息包含评估读数、单位、阈值和持续时间。解除通知保留最后评估时的说明，不会重新测量设备读数。

模板同时处理 `model_group` 这类记录规则标准化标签，以及 `inference_foretoken_io_model_group` 这类 ServiceMonitor 原始标签，因为抓取失败发生在记录规则标准化之前。

## 验证

查看 Operator 接受的资源以及 Alertmanager 路由：

```bash
kubectl get alertmanagerconfig foretoken-lark \
  --namespace "$ALERTMANAGER_NAMESPACE" \
  --output yaml
```

测试告警应携带与正式规则相同的标签，包括 `service=foretoken` 和真实 Foretoken workload namespace。只在 Alertmanager namespace 中测试，不能证明跨 namespace 路由有效。最后应分别确认 Lark 收到了 firing 和 resolved 消息。

## 设计参考

Alertmanager 继续负责告警分组、去重、重复发送和投递。Payload 遍历 `.Alerts`，不假设同组告警一定拥有完全相同的 annotations。

- [Prometheus 通知模板参考](https://prometheus.io/docs/alerting/latest/notifications/)
- [Prometheus Operator `AlertmanagerConfig` API](https://prometheus-operator.dev/docs/api-reference/api/#monitoring.coreos.com/v1alpha1.AlertmanagerConfig)
- [vLLM Production Stack 可观测性配置](https://github.com/vllm-project/production-stack/blob/main/observability/kube-prom-stack.yaml)
- [Dynamo XPU 告警规则](https://github.com/ai-dynamo/dynamo/blob/main/dev/observability/xpu-alert-rules.yml)

Dynamo 的 XPU 规则展示了持续时间、warning/critical 分级、exporter 可用性和“存在业务流量时才检查硬件异常”等做法。vLLM Production Stack 的 receiver 模板会遍历分组后的告警。两者都不会定义 Foretoken 的 Lark 目标、标签或处置流程，因此这些内容仍然是显式的 Foretoken 集成策略。
