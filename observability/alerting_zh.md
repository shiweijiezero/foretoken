<!--
SPDX-License-Identifier: Apache-2.0
SPDX-FileCopyrightText: Copyright contributors to the Foretoken project
-->

# 告警

[English](alerting.md) | 简体中文

Foretoken 提供指标目标不可达、Frontend HTTP 错误、调度队列持续排队、KV Cache 压力，以及加速器利用率、显存、温度和功耗告警。告警与[指标采集](README_zh.md)分别启用：Prometheus 计算告警条件，Alertmanager 负责分组、路由并向平台选择的通知渠道投递。

## 启用告警

使用仓库维护的 [`examples/alerting/platform.yaml`](../examples/alerting/platform.yaml)：

```yaml
observability:
  mode: enabled
alerting:
  enabled: true
```

通过该文件安装或更新平台；如果已有其他平台配置文件，在同一命令中继续传入：

```bash
foretoken install --values examples/alerting/platform.yaml
```

安装后，Prometheus 除采集和记录规则外还会加载 `foretoken.alerting` 告警规则组。这不会修改模型部署，也不会创建通知接收器。若要从当前源码构建并安装，而不是使用发布产物，运行 `foretoken install -e . --values examples/alerting/platform.yaml`。

## 确认规则生效

```bash
kubectl get prometheusrule foretoken-control-plane-alerting-rules \
  --namespace foretoken-platform
```

按照[采集指南](README_zh.md#验证采集)打开 Prometheus。在 **Rules** 中确认 `foretoken.alerting` 已加载且没有计算错误。在 **Alerts** 中，异常条件先进入 pending，持续达到规则要求的时间后进入 firing；条件不再成立时告警解除。各条规则的含义和排查步骤见[告警排障手册](runbooks/alerts_zh.md)。

## 调整阈值与消息语言

修改示例 YAML，无需编辑 Chart 源文件。例如：

```yaml
observability:
  mode: enabled
alerting:
  enabled: true
  language: en
  thresholds:
    acceleratorMemoryUsageRatio: 0.90
    nvidiaTemperatureCelsius: 80
```

硬件阈值包括 `acceleratorUtilizationRatio`、`acceleratorMemoryUsageRatio`、`nvidiaTemperatureCelsius` 和 `nvidiaPowerWatts`。利用率使用 0–1 的比值，温度单位为摄氏度，功耗单位为瓦。未填写的选项沿用平台默认值；应结合设备限制和真实负载测量结果选择阈值。

`language` 支持 `zh`、`en` 和 `bilingual`，通知接收器可据此选择中文、英文或双语消息。规则始终保留中英文 annotations。语言按告警分组生效，不能让同一条消息对不同接收人显示不同语言。

## 通知渠道

邮件、Slack、通用 webhook 等渠道复用 Alertmanager 已有接收器。Foretoken 告警携带 `service=foretoken` 和 `severity=warning` 标签，可据此配置通知路由。

可选的 [Lark 集成](integrations/lark/README_zh.md)提供可直接应用的接收器、Secret 引用和消息模板，复用当前 Alertmanager，不会创建第二套通知服务。

规则正常计算不代表通知已经送达。配置新接收器后，应发送携带真实告警标签和 workload namespace 的受控测试告警，并分别确认接收端收到了触发与解除消息。

## 关闭告警

在同一个配置文件中设置 `alerting.enabled: false`，重新执行安装命令。这只移除 Foretoken 告警规则，指标采集、记录规则和 Dashboard 仍然保留。平台已有的接收器、凭据、静默和通知策略不会删除。执行 `foretoken uninstall` 时，告警规则随所属 release 一起移除。

使用旧配置时，将 `observability.alerts.language` 和 `observability.alerts.thresholds` 移到顶层 `alerting`，并设置 `alerting.enabled: true`，即可继续启用告警。
