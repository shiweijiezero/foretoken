<!--
SPDX-License-Identifier: Apache-2.0
SPDX-FileCopyrightText: Copyright contributors to the Foretoken project
-->

# 可观测性示例

[English](README.md) | 简体中文

在[快速开始](../quickstart/README_zh.md)的模型服务之上启用指标、Grafana 看板和告警。`observability.yaml` 保存告警阈值和通知语言；指标采集和看板不需要配置。在仓库根目录运行：

```bash
foretoken install --values examples/observability/observability.yaml
foretoken deploy examples/quickstart
```

发送几个请求后打开 Grafana，选择 **Foretoken System Overview**，告警阈值会以虚线显示在对应面板上。如何找到 Grafana、接入已有监控和理解各条告警，见[可观测性指南](../../observability/README_zh.md)。
