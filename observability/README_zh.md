<!--
SPDX-License-Identifier: Apache-2.0
SPDX-FileCopyrightText: Copyright contributors to the Foretoken project
-->

# 可观测性

[English](README.md) | 简体中文

Foretoken 使用 Prometheus 采集服务和加速器指标，通过 Grafana 看板 **Foretoken System Overview** 展示。告警规则按需启用，默认关闭。

## 快速开始

```bash
foretoken install
foretoken deploy examples/quickstart
```

`foretoken install` 会复用集群中已有的 Prometheus，没有时安装一套由 CLI 管理的 kube-prometheus-stack。CLI 管理的 Grafana 会自动加载看板。先获取自动生成的管理员凭据，再通过集群提供的地址打开 Grafana：

```bash
GRAFANA_USER="$(kubectl get secret \
  --namespace foretoken-platform \
  foretoken-prometheus-grafana \
  --output jsonpath='{.data.admin-user}' | base64 --decode)"
GRAFANA_PASSWORD="$(kubectl get secret \
  --namespace foretoken-platform \
  foretoken-prometheus-grafana \
  --output jsonpath='{.data.admin-password}' | base64 --decode)"
printf 'Grafana user: %s\nGrafana password: %s\n' \
  "$GRAFANA_USER" "$GRAFANA_PASSWORD"
```

在 Grafana 中进入 **Dashboards**，选择 **Foretoken System Overview**。它沿着请求链路依次展示 Frontend、模型服务、缓存和加速器，最后是扩缩容决策；路由和控制面的细节放在折叠分区里。可以按命名空间、Frontend 服务、模型组、模型角色、模型或模型服务筛选。

## 确认采集正常

```bash
kubectl get servicemonitor,prometheusrule -A \
  -l app.kubernetes.io/name=foretoken-control-plane
```

在 Prometheus 的 Targets 页面确认 Foretoken target 为 `UP`，在 Rules 页面确认 `foretoken.recording`、`foretoken.accelerator-recording` 和 `foretoken.alerting` 已加载。下面的查询返回 Frontend 请求速率：

```promql
sum(foretoken:frontend_http_response_starts:rate5m)
```

## 安装监控

运行 `foretoken install`，CLI 会检测集群中的监控栈并配置 Foretoken 所需的指标采集。检测到沐曦 GPU 资源时，CLI 会验证兼容的 mxExporter 及其指标采集。安装完成后，在 Prometheus 的 Targets 页面确认 Foretoken target 为 `UP`；CLI 管理的 Grafana 会加载 Foretoken System Overview 看板。

## 告警

告警随服务部署配置。在 `ModelService` 中，只选择这个模型需要的规则：

```yaml
spec:
  observability:
    alerts:
      rules:
        - ForetokenMetricsTargetDown
```

[可观测性示例](../examples/observability/README_zh.md)把这些配置放在快速开始的 Kustomize 补丁中。修改其中的 `observability.yaml` 后部署：

```bash
foretoken deploy examples/observability --timeout 20m
```

`FrontendService` 使用相同的配置位置选择前端抓取失败和 HTTP 错误告警。模型规则只覆盖该 ModelService 的执行实例；共享前端的错误仍归前端，不记到某个模型上。可选名称和触发条件见[告警参考](runbooks/alerts_zh.md)。

移除名称或设为 `rules: []`，再次部署即可关闭对应告警，指标和看板仍保留。CLI 会报告告警配置失败，服务自身的就绪状态单独维护；`deploy` 不负责安装监控平台。

选择功耗告警时，还需按显卡型号填写正数 `spec.observability.alerts.thresholds.nvidiaPowerWatts`，单位为瓦。只填写阈值不会启用规则。通知语言、接收目标和时区在接收器上配置，见可选的 [Lark 集成](integrations/lark/README_zh.md)。

## 指标参考

| 来源 | 内容 |
| --- | --- |
| Frontend `/metrics` | HTTP 请求、准入队列、路由和运行状态 |
| model-server `/metrics` | 推理引擎指标和 RuntimeCache 文件系统状态 |
| Controller `/metrics` | Reconcile、工作队列和已发布的扩缩容决策 |
| DCGM Exporter | NVIDIA 利用率、显存、功耗、温度和 XID 错误 |
| mxExporter | 沐曦利用率和显存 |
| kubelet/cAdvisor | 容器 CPU 和内存 |

看板和告警查询下列记录规则。模型服务相关规则来自 vLLM 指标。

| 类别 | 记录规则 | 含义 |
| --- | --- | --- |
| Frontend | `foretoken:frontend_up:sum` | 正在上报的 Frontend target 数量 |
| Frontend | `foretoken:frontend_http_response_starts:rate5m` | 每秒开始的 HTTP 响应数 |
| Frontend | `foretoken:frontend_http_response_start_5xx_ratio:rate5m` | 响应开始时状态为 5xx 的比例 |
| Frontend | `foretoken:frontend_http_response_start_latency_seconds:quantile5m` | 到响应头发出为止的时间，分 `p50`、`p90`、`p99` |
| Frontend | `foretoken:frontend_upstream_queued_requests:sum` | 按扩缩容目标统计的准入等待请求数 |
| Frontend | `foretoken:frontend_kv_index_source_health_ratio:min` | Frontend 副本中最低的 KV 事件源健康比例 |
| 模型服务 | `foretoken:model_server_up:sum` | 正在上报的 model-server target 数量 |
| 模型服务 | `foretoken:model_server_completed_requests:rate5m` | 按结束原因统计的每秒完成请求数 |
| 模型服务 | `foretoken:model_server_prompt_tokens:rate5m` | 每秒处理的 prompt token 数 |
| 模型服务 | `foretoken:model_server_generation_tokens:rate5m` | 每秒生成的 token 数 |
| 模型服务 | `foretoken:model_server_requests_running:sum` | 正在运行的请求数 |
| 模型服务 | `foretoken:model_server_requests_waiting:sum` | 调度器中等待的请求数 |
| 模型服务 | `foretoken:model_server_e2e_request_latency_seconds:quantile5m` | 从 Frontend handler 开始处理到生成完成的时间，分 `p50`、`p90`、`p99` |
| 模型服务 | `foretoken:model_server_time_to_first_token_seconds:quantile5m` | 首 token 延迟，分 `p50`、`p90`、`p99` |
| 模型服务 | `foretoken:model_server_time_per_output_token_seconds:quantile5m` | 每输出 token 的时间，分 `p50`、`p90`、`p99` |
| 模型服务 | `foretoken:model_server_inter_token_latency_seconds:quantile5m` | 相邻输出 token 之间的间隔，分 `p50`、`p90`、`p99` |
| 模型服务 | `foretoken:model_server_request_stage_time_seconds:quantile5m` | 请求在 `queue`、`prefill`、`decode` 各阶段的时间，分 `p50`、`p90`、`p99` |
| 模型服务 | `foretoken:model_server_preemptions:rate5m` | 每秒被抢占的请求数 |
| 模型服务 | `foretoken:model_server_request_prompt_tokens_bucket:rate5m` | prompt 长度直方图桶 |
| 模型服务 | `foretoken:model_server_request_generation_tokens_bucket:rate5m` | 输出长度直方图桶 |
| 缓存 | `foretoken:model_server_kv_cache_usage_ratio:max` | 引擎中最高的 KV Cache 使用率 |
| 缓存 | `foretoken:model_server_prefix_cache_hit_ratio:rate5m` | 本地或外部 Prefix Cache 命中率 |
| 缓存 | `foretoken:model_server_runtime_cache_available_bytes:min` | RuntimeCache 最少的剩余空间 |
| 缓存 | `foretoken:model_server_runtime_cache_usage_ratio:max` | RuntimeCache 最高的使用率 |
| 缓存 | `foretoken:model_server_runtime_cache_observation_success:min` | 是否所有 RuntimeCache 挂载都能被检查 |
| 缓存 | `foretoken:model_server_runtime_cache_temporary:max` | 是否有 model-server 在使用 Pod 内的临时缓存 |
| 加速器 | `foretoken:accelerator_gpu_utilization_ratio` | 每块 NVIDIA 或沐曦设备的利用率 |
| 加速器 | `foretoken:accelerator_gpu_memory_usage_ratio` | 每块 NVIDIA 或沐曦设备的显存使用率 |
| 加速器 | `foretoken:accelerator_gpu_power_watts` | 每块 NVIDIA 设备的功耗 |
| 加速器 | `foretoken:accelerator_gpu_temperature_celsius` | 每块 NVIDIA 设备的温度 |

记录规则保留命名空间、Frontend 服务、模型组、模型角色、模型名称和 Prefill/Decode pipeline scope 标签。Frontend 延迟在响应头发出时结束，流式响应的 token 发送时间不计入；生成完成延迟和首 token 延迟都从 JSON 解码后的 Frontend handler 入口开始计时，跨进程测量要求节点时钟同步。流式响应可能先以 `2xx` 开始、之后再失败，因此 5xx 比例不是推理成功率。加速器规则只覆盖 Foretoken 工作负载使用的设备。

## 性能剖析

对已有诊断服务采集一段 CPU/GPU 执行时间线，见[性能剖析指南](profiling_zh.md)。该功能独立于指标采集。

## 停止采集

删除全部 Foretoken 服务后，`foretoken uninstall` 会删除由 CLI 管理的 Prometheus 和 DCGM Exporter release。复用的 Prometheus、DCGM Exporter 和 mxExporter 保持不变。
