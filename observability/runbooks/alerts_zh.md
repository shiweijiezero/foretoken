<!--
SPDX-License-Identifier: Apache-2.0
SPDX-FileCopyrightText: Copyright contributors to the Foretoken project
-->

# Foretoken 告警排障手册

[English](alerts.md) | 简体中文

Foretoken 告警表示异常信号已经持续了一段时间。告警不会自动修复系统，也不能单独证明用户请求已经中断。排查时先读取告警标签，再用当前 Kubernetes 状态确认信号。

所有规则集中在一个源文件中；先用下表选择对应的排障章节，不需要为每种算法寻找单独文件：

| 告警 | 信号 | 默认持续时间 |
| --- | --- | --- |
| `ForetokenMetricsTargetDown` | 已发现的 Frontend 或 model-server `/metrics` 目标无法抓取 | 5 分钟 |
| `ForetokenFrontendHTTPResponseStart5xxRatioHigh` | 在存在流量时，Frontend 响应开始 5xx 比例偏高 | 10 分钟 |
| `ForetokenModelServerSchedulerBacklog` | 聚合后的 vLLM stage scheduler waiting 队列不为空 | 10 分钟 |
| `ForetokenModelServerKVCachePressureHigh` | vLLM KV Cache 最高使用率偏高 | 10 分钟 |
| `ForetokenAcceleratorGPUUtilizationHigh` | NVIDIA 或沐曦的标准化 GPU 利用率偏高 | 15 分钟 |
| `ForetokenNVIDIAGPUTemperatureHigh` | NVIDIA GPU 温度超过配置阈值 | 10 分钟 |
| `ForetokenNVIDIAGPUPowerUsageHigh` | NVIDIA GPU 功耗超过配置阈值 | 10 分钟 |

首先查看告警所在命名空间中的资源：

```bash
NAMESPACE=foretoken-demo
kubectl get pods,services,endpointslices --namespace "$NAMESPACE" -o wide
```

如果某条记录指标消失，应先检查原始 `up` 指标和 Pod 状态。指标缺失表示数据不可用，不等于数值为零。

加速器告警使用由 exporter 的模型组、模型角色 Pod 标签归属到 Foretoken 工作负载的记录指标。不带这些标签的样本不参与告警评估，温度和功耗告警也遵循这一范围。

## ForetokenMetricsTargetDown

Prometheus 已连续五分钟无法抓取一个已经发现的 Frontend 或 model-server 目标。

1. 打开 Prometheus Targets 页面，查看该目标的 `lastError`。
2. 检查对应 Pod 是否运行，并从监控 Pod 测试其 `/metrics` 接口。
3. 检查 Service endpoint、命名端口、ServiceMonitor selector 和 NetworkPolicy。
4. 如果应用本身不健康，检查日志和最近的发布事件。

这条告警只检查指标采集是否可达。它无法发现已经完全退出服务发现的目标；`up == 0` 也不等于请求服务未就绪或已经发生用户故障。

## ForetokenFrontendHTTPResponseStart5xxRatioHigh

Frontend 的 HTTP 响应开始事件中，5xx 比例在至少每秒 0.1 个响应开始事件的流量下连续十分钟超过 5%。

1. 按告警中的 namespace 和 Frontend service 筛选记录指标，再按 handler 和 status 拆分。
2. 检查 Frontend 日志以及最近的配置或路由修改。
3. 如果对应 handler 会执行推理，再检查后端是否可用。

该指标在 HTTP response 开始时记录状态。后续流式传输即使失败，也可能已经记录为 2xx，因此它不是推理失败率或 SLO。

## ForetokenModelServerSchedulerBacklog

聚合后的 vLLM stage scheduler 队列连续十分钟不为空。

1. 查看告警所指 model group 和 role 的 running 与 waiting requests。
2. 检查 KV Cache 压力、Pod 健康状态、加速器利用率和近期流量变化。
3. 分别检查受影响的 prefill 或 decode role，不要把 stage 数量解释为用户请求数。

这是 vLLM 的 stage 队列，不是用户数量，也不是 Frontend admission queue。短暂排队属于正常现象，所以规则要求 backlog 连续存在十分钟。

## ForetokenModelServerKVCachePressureHigh

一个 model group 中所有 engine 的最高 KV Cache 使用率连续十分钟不低于配置的阈值（默认 95%）。

1. 确认 group、role 和 model 标签，再查看 scheduler waiting 与 running requests。
2. 调整容量前，先检查请求长度、并发、workload 配置和副本健康状态。
3. 比较各个 Pod 或 engine，定位实际热点。

记录指标在每次计算时取所有 engine 的最大值，而不是集群平均值；贡献最大值的 engine 可能随时间改变。阈值由 `observability.alerts.thresholds.kvCacheUsageRatio` 配置，应根据真实 workload 的测量结果调整。

## ForetokenAcceleratorGPUUtilizationHigh

标准化的 NVIDIA 或沐曦利用率已超过配置阈值。扩容前先检查近期请求量、scheduler waiting 与 running requests，以及受影响的节点或设备。

## ForetokenNVIDIAGPUTemperatureHigh

NVIDIA DCGM 温度读数已超过配置阈值。检查节点散热、设备健康、功耗和 workload 放置情况。没有归属到 Foretoken 的 NVIDIA DCGM 温度指标时，这条规则不会产生告警。

## ForetokenNVIDIAGPUPowerUsageHigh

NVIDIA DCGM 功耗读数已超过配置阈值。先将读数与设备功耗上限和 workload 对比，再检查温度和节点健康状态；没有归属到 Foretoken 的 NVIDIA DCGM 功耗指标时，这条规则不会产生告警。

## GPU 阈值策略

Chart 为标准化利用率以及 NVIDIA 温度和功耗读数提供默认阈值。安装 Chart 时可以通过 `observability.alerts.thresholds` 覆盖这些值。利用率规则覆盖 NVIDIA 与沐曦的标准化指标；温度和功耗规则只在集群存在 NVIDIA DCGM 指标时生效。这些告警用于容量和散热检查，不会自动修复系统。
