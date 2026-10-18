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

重新部署服务配置后生效。移除规则或设为 `rules: []` 即可关闭。完整可运行配置见[可观测性示例](../../examples/feature/observability/README_zh.md)。

配置 [Lark](../integrations/lark/README_zh.md)、[Slack](../integrations/slack/README_zh.md) 或[钉钉](../integrations/dingtalk/README_zh.md)接收器后，可接收通知。所选规则未出现在 Prometheus 中时，查看服务的 `AlertsReady` 状态。

## 选择规则

定位告警时，在 [Grafana](../README_zh.md#查看指标) 打开 Foretoken 系统概览，选择通知中的命名空间和前端或模型服务。

下表中的阈值字段均位于 `spec.observability.alerts.thresholds` 下，准入字段放在其中的 `admission` 下。三条准入比例／延迟规则需要显式填写阈值和正数最小速率，没有默认值。它们共用可选的 `scope`、`window` 和 `for`：`scope` 默认 `service`，按模型汇总所选前端的各 Pod；设为 `pod` 时，按模型与 Pod 分别评估。`window` 是计算窗口，默认 `1m`；`for` 是触发前条件须持续的时间，默认 `5m`。时长支持整数秒、分钟或小时。

两条准入比例均以该模型所有已结束的 HTTP 准入调用为分母，包括失败和仅做 CPU 输入准备的调用。调用在首次后端接受、输入准备完成或最终失败时结束，不是生成完成时。统计按调用次数计，不按候选工作单元计，也不包含内部调用。

| 规则 | 配置在哪类服务 | 触发条件与阈值 | 优先检查 |
| --- | --- | --- | --- |
| `ForetokenMetricsTargetDown` | 前端或模型服务 | 指标端点连续 1 分钟无法抓取；无可配置阈值。 | 查看 Prometheus Targets 中的错误，再检查对应 Pod 和指标端点的网络访问。 |
| `ForetokenFrontendHTTPResponseStart5xxRatioHigh` | 前端服务 | 5 分钟窗口内，每秒至少 0.1 次 HTTP 响应开始时，5xx 比例连续 2 分钟超过 5%；无可配置阈值。 | 查看状态码趋势和前端日志，再检查准入拒绝／超时、模型可用性及后端错误。 |
| `ForetokenAdmissionCapacityRejectionRatioHigh` | 前端服务 | 容量拒绝比例超过 `admission.capacityRejectionRatio`（0–1），且已结束调用速率 ≥ `admission.minResultRate`（调用次数/秒，正数）。 | 对照各 Pod 的派发结果、共享等待占用、调用方限额和后端负载，再决定是否调整限额。 |
| `ForetokenAdmissionTimeoutRatioHigh` | 前端服务 | `queue_timeout` 与 `deadline_exceeded` 合计比例超过 `admission.timeoutRatio`（0–1），且已结束调用速率 ≥ `admission.minResultRate`（调用次数/秒，正数）。 | 对照占用、等待时间、`queueTimeout` 和请求超时，区分队列等待到期与获准前请求预算耗尽。 |
| `ForetokenAdmissionAdmittedQueueP95High` | 前端服务 | 等待时间 p95 超过 `admission.admittedQueueP95Seconds`（秒，正数），且样本调用速率 ≥ `admission.minQueuedAdmissionRate`（调用次数/秒，正数）。 | 结合等待时间、共享等待占用和上限定位延迟；查看超时结果，判断请求是否在后端接受前退出。 |
| `ForetokenAdmissionTelemetryMissing` | 前端服务 | 抓取成功，但必要准入指标缺失或模型清单不一致，持续 5 分钟；始终按 Pod 评估。 | 查看“准入”副本表，核对 Pod 运行版本和监控配置。 |
| `ForetokenNVIDIAGPUTemperatureHigh` | 模型服务 | NVIDIA GPU 温度 ≥ `nvidiaTemperatureCelsius`（°C，默认 85），持续 2 分钟。 | 按通知中的设备检查温度、散热和当前负载。 |
| `ForetokenNVIDIAGPUPowerUsageHigh` | 模型服务 | NVIDIA GPU 功耗 ≥ `nvidiaPowerWatts`（瓦，必填正数，无默认值），持续 5 分钟。 | 比较设备功耗、负载、预期运行范围及告警阈值。 |

等待时间 p95 统计 HTTP 调用从取得等待名额至首次后端接受的耗时，包括输入准备和提交；不包含无限流、仅做 CPU 输入准备或超时的调用。`minQueuedAdmissionRate` 也按这一样本范围计算。
