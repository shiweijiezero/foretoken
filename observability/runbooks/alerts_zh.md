<!--
SPDX-License-Identifier: Apache-2.0
SPDX-FileCopyrightText: Copyright contributors to the Foretoken project
-->

# Foretoken 告警参考

[English](alerts.md) | 简体中文

在所属服务的 `spec.observability.alerts.rules` 中选择规则。列表为空表示关闭告警。Prometheus 必须发现工作负载命名空间和所选 `PrometheusRule`；未选择告警时指标仍可用。

| 告警 | 服务类型 | 触发条件 | 持续时间 | 必要阈值或作用域 |
| --- | --- | --- | --- | --- |
| `ForetokenMetricsTargetDown` | `FrontendService` 或 `ModelService` | 已发现的 `/metrics` 目标无法抓取 | 1 分钟 | 所选服务的目标 |
| `ForetokenFrontendHTTPResponseStart5xxRatioHigh` | `FrontendService` | 流量至少为每秒 0.1 个响应开始事件时，5xx 比例超过 5% | 2 分钟 | 所选 FrontendService |
| `ForetokenNVIDIAGPUTemperatureHigh` | `ModelService` | 归属到该服务的 NVIDIA GPU 达到温度阈值 | 2 分钟 | `nvidiaTemperatureCelsius`（默认 85°C）；ModelGroup 作用域 |
| `ForetokenNVIDIAGPUPowerUsageHigh` | `ModelService` | 归属到该服务的 NVIDIA GPU 达到功耗阈值 | 5 分钟 | 选择该规则并设置正数 `nvidiaPowerWatts`；ModelGroup 作用域 |

GPU 温度和功耗规则只使用归属到所选 ModelService 的 ModelGroup 记录指标。当前不提供 GPU 利用率和显存占用告警，但 Dashboard 仍保留这些指标。

抓取恢复或目标退出服务发现时，`ForetokenMetricsTargetDown` 都会解除。

通知接入见 [Lark](../integrations/lark/README_zh.md)、[Slack](../integrations/slack/README_zh.md) 或 [钉钉](../integrations/dingtalk/README_zh.md) 集成。
