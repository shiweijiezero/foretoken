<!--
SPDX-License-Identifier: Apache-2.0
SPDX-FileCopyrightText: Copyright contributors to the Foretoken project
-->

# 服务告警

[English](alerts.md) | 简体中文

为前端或模型服务启用告警，再根据通知定位受影响的服务或设备。

## 启用告警

在服务的 `spec` 下添加所选规则，例如：

```yaml
spec:
  observability:
    alerts:
      rules:
        - ForetokenMetricsTargetDown
```

重新部署服务配置后生效。移除规则或设为 `rules: []` 即可关闭。完整可运行配置见[可观测性示例](../../examples/observability/README_zh.md)。

配置 [Lark](../integrations/lark/README_zh.md)、[Slack](../integrations/slack/README_zh.md) 或[钉钉](../integrations/dingtalk/README_zh.md)接收器后，可接收通知。所选规则未出现在 Prometheus 中时，查看服务的 `AlertsReady` 状态。

## 选择规则

| 规则 | 配置在哪类服务 | 触发条件 |
| --- | --- | --- |
| [ForetokenMetricsTargetDown](#foretokenmetricstargetdown) | 前端或模型服务 | 指标端点连续 1 分钟无法抓取。 |
| [ForetokenFrontendHTTPResponseStart5xxRatioHigh](#foretokenfrontendhttpresponsestart5xxratiohigh) | 前端服务 | 5 分钟窗口内，每秒至少 0.1 次 HTTP 响应开始时，5xx 比例连续 2 分钟超过 5%。 |
| [ForetokenAdmissionCapacityRejectionRatioHigh](#foretokenadmissioncapacityrejectionratiohigh) | 前端服务 | 准入容量拒绝比例超过配置阈值。 |
| [ForetokenAdmissionTimeoutRatioHigh](#foretokenadmissiontimeoutratiohigh) | 前端服务 | 准入超时比例超过配置阈值。 |
| [ForetokenAdmissionAdmittedQueueP95High](#foretokenadmissionadmittedqueuep95high) | 前端服务 | 排队后获准请求的等待 p95 超过配置时长。 |
| [ForetokenAdmissionTelemetryMissing](#foretokenadmissiontelemetrymissing) | 前端服务 | 抓取成功，但必要准入指标连续缺失 5 分钟。 |
| [ForetokenNVIDIAGPUTemperatureHigh](#foretokennvidiagputemperaturehigh) | 模型服务 | NVIDIA GPU 温度连续 2 分钟达到阈值，默认 85°C。 |
| [ForetokenNVIDIAGPUPowerUsageHigh](#foretokennvidiagpupowerusagehigh) | 模型服务 | NVIDIA GPU 功耗连续 5 分钟达到配置阈值。 |

准入阈值写在 `spec.observability.alerts.thresholds.admission` 下。三条比例或延迟告警需要显式填写阈值和正数最小速率。可选 `scope` 默认 `service`，可设为 `pod`；`window` 默认 `1m`，`for` 默认 `5m`。时长支持整数秒、分钟或小时，分别控制聚合范围、计算窗口及触发告警前条件须持续的时间。

## 定位告警原因

在 [Grafana](../README_zh.md#查看指标) 打开 Foretoken 系统概览，选择通知中的命名空间和前端或模型服务。

### ForetokenMetricsTargetDown

查看 Prometheus Targets 中的抓取错误，再检查对应 Pod 和指标端点的网络访问。

### ForetokenFrontendHTTPResponseStart5xxRatioHigh

查看前端状态码趋势和日志。通过“准入”区域定位容量拒绝、超时响应，再检查模型可用性和后端错误。

### ForetokenAdmissionCapacityRejectionRatioHigh

在准入阈值下填写 0 至 1 的 `capacityRejectionRatio`，以及以已结束调用次数/秒为单位的 `minResultRate`。拒绝比例按准入阶段分别计算，以该阶段已结束的调用为分母。

比较各前端 Pod 的流量、占用和配置上限。`intake` 对应 HTTP 驻留名额，`work` 对应工作准入；调整前端限额前，同时查看后端负载。

### ForetokenAdmissionTimeoutRatioHigh

在准入阈值下填写 0 至 1 的 `timeoutRatio`，以及以已结束调用次数/秒为单位的 `minResultRate`。分子为 `queue_timeout` 和 `deadline_exceeded`，分母为已结束的工作准入调用。

对照队列占用、等待时间、`queueTimeout` 和请求超时。结果分类可区分队列等待到期与获准前请求预算耗尽。

### ForetokenAdmissionAdmittedQueueP95High

在准入阈值下填写正数 `admittedQueueP95Seconds`（秒）和 `minQueuedAdmissionRate`（调用次数/秒）。此规则只统计实际排队后获准的请求，不包含直接获准或等待超时的请求。

结合获准等待曲线、队列占用和模型容量定位延迟，同时查看超时结果，判断是否还有请求等待后未能获准。

### ForetokenAdmissionTelemetryMissing

在准入副本表中定位指标不完整的 Pod，并核对运行版本。升级完成后仍未恢复时，检查监控配置。

### ForetokenNVIDIAGPUTemperatureHigh

修改默认阈值时，设置 `spec.observability.alerts.thresholds.nvidiaTemperatureCelsius`，单位为 °C。

按通知中的设备检查温度、散热和当前负载。

### ForetokenNVIDIAGPUPowerUsageHigh

设置正数 `spec.observability.alerts.thresholds.nvidiaPowerWatts`，单位为瓦；此规则没有默认阈值。

比较设备功耗、当前负载、预期运行范围及配置的告警阈值。
