<!--
SPDX-License-Identifier: Apache-2.0
SPDX-FileCopyrightText: Copyright contributors to the Foretoken project
-->

# 可观测性

[English](README.md) | 简体中文

Foretoken 使用 Prometheus 采集服务和加速器指标，通过 Grafana 看板 Foretoken System Overview 展示。告警规则按需启用，默认关闭。

## 快速开始

```bash
foretoken install
foretoken deploy examples/quickstart
```

`foretoken install` 会复用集群中已有的 Prometheus，没有时安装一套由 CLI 管理的 kube-prometheus-stack。如果安装了监控栈，使用 CLI 管理的 Grafana 并获取自动生成的管理员凭据；如果复用已有监控栈，则使用该平台的 Grafana 和凭据：

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

在 Grafana 中打开 Foretoken 系统概览，或英文版 Foretoken System Overview。先选命名空间和模型，再按模型实例、执行角色或引擎 rank 缩小范围。模型服务、缓存、GPU 和路由面板随之筛选。“路由决策”展示同一模型、同一角色内各后端的占比；一个后端对应一个模型实例和数据并行 rank。筛选后端时，分母仍保留该模型该角色的全部后端。

“共享前端”展示所选前端的全部流量，不归属于单个模型。扩缩容按所选模型和服务查看，控制面诊断则反映整个平台。

升级 Foretoken 后，再执行 `foretoken install`，更新控制器、前端、采集配置和看板。只导入看板 JSON 不会更新指标生产端。

## 确认采集正常

```bash
kubectl get servicemonitor,prometheusrule -A \
  -l app.kubernetes.io/name=foretoken-control-plane
```

在 Prometheus 的 Targets 页面确认 Foretoken target 为 `UP`，在 Rules 页面确认 `foretoken.recording` 已加载；启用服务告警时，再确认对应的 `foretoken.alerting` 规则已加载。下面的查询返回 Frontend 请求速率：

```promql
sum(foretoken:frontend_http_response_starts:rate5m)
```

## 接入已有监控

CLI 优先复用集群已有的组件，只安装缺少的部分：

| 组件 | 不存在 | 存在 | 存在但不可用 | `foretoken uninstall` |
| --- | --- | --- | --- | --- |
| Prometheus | 安装 CLI 管理的 kube-prometheus-stack | 复用 | 停止并要求显式指定 | 只删除 CLI 管理的 release |
| NVIDIA DCGM Exporter | 有 NVIDIA GPU 时安装 CLI 管理的 exporter | 复用 | 停止 | 只删除 CLI 管理的 release |
| 沐曦 mxExporter | 安装 CLI 管理的 exporter | 复用 | 停止 | 只删除 CLI 管理的资源 |

exporter 可用的条件是覆盖全部 GPU 节点并被选中的 Prometheus 抓取。CLI 不安装 GPU 驱动、device plugin 或厂商 Operator。

集群中有多个兼容的 Prometheus 时，显式指定一个：

```bash
# 允许该 Prometheus 所在命名空间抓取 Foretoken 指标
kubectl label namespace monitoring \
  inference.foretoken.io/metrics-scraper=true \
  --overwrite

# 指定 Prometheus 实例
foretoken install --prometheus monitoring/prometheus
```

GPU 面板和告警依靠 Foretoken 模型组和模型角色的 Pod 标签识别设备。CLI 管理的 DCGM Exporter 会输出这些标签；复用已有 exporter 时需要同样的标签，否则这些面板没有数据。

使用服务告警时，复用的 Prometheus 需要通过 `ruleNamespaceSelector` 选择工作负载命名空间中的规则；CLI 管理的监控已配置这一范围。

复用 Prometheus 时，Grafana 仍由原平台管理。能够发现 `grafana_dashboard=1` ConfigMap 的 Grafana sidecar 会从 `foretoken-platform` 命名空间自动加载看板；否则导出 JSON 后在 Grafana 中导入：

```bash
kubectl get configmap \
  --namespace foretoken-platform \
  foretoken-control-plane-system-dashboard \
  --output jsonpath='{.data.foretoken-system-overview\.json}' \
  > /tmp/foretoken-system-overview.json
```

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

选择功耗告警时，还需按显卡型号填写正数 `spec.observability.alerts.thresholds.nvidiaPowerWatts`，单位为瓦。只填写阈值不会启用规则。通知接入见 [Lark](integrations/lark/README_zh.md) 或 [Slack](integrations/slack/README_zh.md) 集成。

## 指标参考

| 来源 | 内容 |
| --- | --- |
| Frontend `/metrics` | HTTP 请求、准入队列、路由和运行状态 |
| model-server `/metrics` | 推理引擎指标和 RuntimeCache 文件系统状态 |
| Controller `/metrics` | Reconcile、工作队列和已发布的扩缩容决策 |
| DCGM Exporter | NVIDIA 利用率、显存、功耗、温度和 XID 错误 |
| mxExporter | 沐曦利用率和显存 |
| kubelet/cAdvisor | 容器 CPU 和内存 |

看板中的 TTFT 和 E2EL 使用秒，TPOT 和 ITL 使用毫秒。p50/p95/p99 分位数按模型和角色，合并所选实例的请求直方图后计算。请求速率、token 吞吐量和调度压力按后端展示，一个后端对应一个模型实例和数据并行 rank；概览卡片仍保留模型级总量。前缀缓存命中率使用命中 token 总数除以查询 token 总数，没有查询或指标缺失时不显示比例。路由份额统计选择次数，不代表请求完成率或缓存命中率。

下列记录规则供告警和固定窗口查询使用。模型服务相关规则来自 vLLM 指标。

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

对已有诊断服务采集一段 CPU/GPU 执行时间线，见[性能剖析指南](../benchmarks/docs/profile/README_zh.md)。该功能独立于指标采集。

## 停止采集

删除全部 Foretoken 服务后，`foretoken uninstall` 会删除由 CLI 管理的 Prometheus、DCGM Exporter 和沐曦 mxExporter 资源。复用的安装保持不变。
