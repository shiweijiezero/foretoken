<!--
SPDX-License-Identifier: Apache-2.0
SPDX-FileCopyrightText: Copyright contributors to the Foretoken project
-->

# 告警与 Lark 通知

[可观测性](README_zh.md) | [English](alerting.md)

Foretoken 提供指标和记录规则，但不会安装告警规则，也不会管理通知接收器。告警阈值、Alertmanager 路由和 Lark 投递由集群监控平台的配置负责。

## 定义告警规则

告警应基于[可观测性文档](README_zh.md)中记录的稳定记录规则。例如，平台自己的 Prometheus 规则组可以在没有 Frontend target 上报时触发告警：

```yaml
groups:
  - name: foretoken.platform
    rules:
      - alert: ForetokenFrontendUnavailable
        expr: foretoken:frontend_up:sum == 0
        for: 10m
        labels:
          severity: warning
          team: inference
        annotations:
          summary: No Foretoken Frontend target is reporting
          description: The selected Foretoken Frontend has reported no healthy target for 10 minutes.
```

`PrometheusRule` 的元数据、选择器标签、评估间隔和归属取决于集群使用的 Prometheus Operator。请通过平台自己的配置仓库或 GitOps 流程加入规则，而不要修改 Foretoken Helm chart。阈值和 `for` 时长应根据服务运行目标选择；上例只是起点，不是 Foretoken 默认值。

流式响应可能先以 `2xx` 开始、后续再失败，因此不要把 `foretoken:frontend_http_response_start_5xx_ratio:rate5m` 当作推理成功率 SLO。定义故障条件时，应将响应开始信号与服务和模型服务信号结合起来。

## 通过 Lark 投递通知

Foretoken 没有内置的 Lark 接收器。要将告警发送到 Lark：

1. 在集群监控平台的 Alertmanager 集成中配置 Lark 兼容的 webhook 或通知连接器。
2. 按平台要求将 webhook URL、签名密钥等凭据存入平台的 Secret 管理，不要写入 Foretoken values、示例或 Git。
3. 将平台告警标签（例如 `team` 和 `severity`）路由到该接收器。
4. 通过平台正常的 Alertmanager 流程触发受控测试告警，确认 Lark 中的投递、去重和恢复通知。

具体接收器和 Secret 格式取决于集群使用的 Alertmanager 集成。该集成应留在 Foretoken 之外，这样 `foretoken uninstall` 不会删除平台拥有的通知策略。

## 归属与清理

平台团队负责告警表达式、阈值、静默、升级策略、Alertmanager 接收器和 Lark 访问权限。Foretoken 负责随平台发布安装的指标与记录规则。删除 Foretoken 服务并运行 `foretoken uninstall` 不会删除平台拥有的告警规则或 Lark 集成。
