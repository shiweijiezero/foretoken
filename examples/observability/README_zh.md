<!--
SPDX-License-Identifier: Apache-2.0
SPDX-FileCopyrightText: Copyright contributors to the Foretoken project
-->

# 启用服务告警

[English](README.md) | 简体中文

为快速开始的模型和前端启用服务告警。示例沿用[快速开始](../quickstart/README_zh.md)的资源、命名空间和数据目录。

## 选择并部署告警

按仓库[快速开始](../../README_zh.md#快速开始)安装平台。在 [`observability.yaml`](observability.yaml) 的前端或模型服务中，将 `rules: []` 换成需要的规则。例如，启用指标端点不可用告警：

```yaml
spec:
  observability:
    alerts:
      rules:
        - ForetokenMetricsTargetDown
```

两份列表默认均为空，前端和模型规则分别选择。规则的触发条件和所需阈值见[告警参考](../../observability/runbooks/alerts_zh.md)。

从仓库根目录部署：

```bash
foretoken deploy examples/observability --timeout 20m
```

按[快速开始的请求示例](../quickstart/README_zh.md)发送请求。在 [Grafana](../../observability/README_zh.md#查看指标) 打开 Foretoken 系统概览并选择 `foretoken-demo`，再到 Prometheus Alerts 页面确认所选规则。配置[通知接收器](../../observability/README_zh.md#告警)后，可接收触发和解除消息。

## 关闭告警或删除服务

从 `observability.yaml` 移除规则并重新部署，即可关闭该告警。删除模型和前端部署：

```bash
foretoken delete examples/observability
```

共享监控仍保留。
