<!--
SPDX-License-Identifier: Apache-2.0
SPDX-FileCopyrightText: Copyright contributors to the Foretoken project
-->

# 可观测性

[English](README.md) | 简体中文

Foretoken 使用 Prometheus 采集服务与加速器指标，并通过 Foretoken System Overview Dashboard 展示。记录规则将原始采样聚合为 Dashboard 使用的查询结果。[告警](alerting_zh.md)是单独启用的可选功能。

## 安装采集

```bash
foretoken install
```

CLI 会发现采集路径，并在修改集群前打印安装计划。如需自定义采集设置，修改 [`examples/observability/platform.yaml`](../examples/observability/platform.yaml)，运行 `foretoken install --values examples/observability/platform.yaml`。模型服务仍使用原有示例 YAML 和 `foretoken deploy`。

| 组件 | 没有合格实例 | 有合格实例 | 实例冲突或链路不完整 | `foretoken uninstall` |
| --- | --- | --- | --- | --- |
| Prometheus | 安装由 CLI 管理的 kube-prometheus-stack | 复用 | 停止，并要求显式选择或修复 | 只删除由 CLI 管理的 release |
| NVIDIA DCGM Exporter | 存在 NVIDIA GPU 时安装由 CLI 管理的 exporter | 复用 | 停止 | 只删除由 CLI 管理的 release |
| 沐曦 mxExporter | 停止；集群必须提供该组件 | 复用 | 停止 | 保留 |

合格的 exporter 必须就绪、覆盖全部选中的 GPU 节点，并且具有被 Prometheus 选择的唯一 ServiceMonitor。CLI 不安装 GPU 驱动、device plugin 或厂商 Operator。

自动发现得到多个兼容 Prometheus 时，显式选择一个：

```bash
# 允许 Prometheus 所在命名空间采集 Foretoken 指标
kubectl label namespace monitoring \
  inference.foretoken.io/metrics-scraper=true \
  --overwrite

# 选择 Prometheus 实例
foretoken install --prometheus monitoring/prometheus
```

命名空间标签由 Prometheus 所属平台管理，不再采集时也通过该平台移除。

## 验证采集

查看 Foretoken 的 ServiceMonitor 和 PrometheusRule：

```bash
# 查看 Foretoken 的 ServiceMonitor 和记录规则
kubectl get servicemonitor,prometheusrule -A \
  -l app.kubernetes.io/name=foretoken-control-plane
```

通过监控平台提供的 Prometheus 地址打开 **Targets** 页面，确认 Foretoken target 为 `UP`；再打开 **Rules** 页面，确认 `foretoken.recording` 已加载。访问入口由监控平台通过 Ingress、Gateway 或可达的 Service 提供，安装采集不会额外开放公网端点。

以下查询可以查看 Frontend 请求量：

```promql
sum(foretoken:frontend_http_response_starts:rate5m)
```

## 打开 Grafana Dashboard

由 CLI 管理的 kube-prometheus-stack 会自动加载 **Foretoken System Overview**。通过监控平台配置的地址打开 Grafana；CLI 管理的实例如果未接入平台单点登录，可获取自动生成的管理员凭据：

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

在 Grafana 中进入 **Dashboards**，选择 **Foretoken System Overview**。这一套 Dashboard 按请求链路依次展示 Frontend 流量和准入、model-server 延迟与吞吐、调度状态、KV Cache 与 RuntimeCache、加速器利用率和服务容器资源。页面可按工作负载命名空间、Frontend 服务、模型组、模型角色、模型和自动扩缩容的模型服务筛选。路由面板展示选择结果、候选数量和各阶段耗时；控制面面板展示 reconcile 与队列状态；扩缩容面板对照建议副本、已应用目标、服务容量、观测年龄和决策原因。控制面与加速器面板展示整个平台，不随工作负载命名空间筛选。

如果 Foretoken 复用已有 Prometheus，Grafana 仍由原平台管理。能够发现 `grafana_dashboard=1` ConfigMap 的 Grafana sidecar 可以从 `foretoken-platform` 命名空间自动加载该 Dashboard。否则先导出 JSON，再按照平台已有流程导入：

```bash
kubectl get configmap \
  --namespace foretoken-platform \
  foretoken-control-plane-system-dashboard \
  --output jsonpath='{.data.foretoken-system-overview\.json}' \
  > /tmp/foretoken-system-overview.json
```

## 指标与记录规则

| 来源 | 内容 |
| --- | --- |
| Frontend `/metrics` | HTTP 请求、准入队列、路由和运行状态 |
| model-server `/metrics` | 当前推理后端的原生指标和已挂载 RuntimeCache 的文件系统状态 |
| Controller `/metrics` | Reconcile、工作队列和已发布的模型服务扩缩容决策 |
| DCGM Exporter | NVIDIA 利用率、显存、功耗、温度和 XID 错误 |
| mxExporter | 沐曦利用率和显存指标 |
| kubelet/cAdvisor | 容器 CPU、内存、文件系统和网络 |
| kube-state-metrics | Kubernetes 资源状态 |

以下稳定记录规则构成系统级 Dashboard 使用的查询层。模型服务规则目前基于 vLLM 指标族生成，不是其他推理后端的统一指标契约。

| 范围 | 记录规则 | 含义 |
| --- | --- | --- |
| Frontend | `foretoken:frontend_up:sum` | 正在上报的 Frontend target 数量 |
| Frontend | `foretoken:frontend_http_response_starts:rate5m` | 每秒开始的 HTTP 响应数 |
| Frontend | `foretoken:frontend_http_response_start_5xx_ratio:rate5m` | 响应开始时的 5xx 比例，不是推理失败率 |
| Frontend | `foretoken:frontend_http_response_start_latency_seconds:quantile5m` | 从入口到 handler 生成响应头的时间，通过 `quantile` 标签区分 `p50`、`p90` 和 `p99`；不包含 SSE body 的发送 |
| Frontend | `foretoken:frontend_upstream_queued_requests:sum` | 按扩缩容目标统计的准入等待请求数 |
| Frontend | `foretoken:frontend_kv_index_source_health_ratio:min` | Frontend 副本中最低的 KV 事件源健康比例 |
| 模型服务 | `foretoken:model_server_up:sum` | 正在上报的 model-server target 数量 |
| 模型服务 | `foretoken:model_server_completed_requests:rate5m` | 按结束原因统计的每秒完成请求数 |
| 模型服务 | `foretoken:model_server_prompt_tokens:rate5m` | 每秒处理的输入 Token 数 |
| 模型服务 | `foretoken:model_server_generation_tokens:rate5m` | 每秒生成的输出 Token 数 |
| 模型服务 | `foretoken:model_server_requests_running:sum` | 当前运行中的请求数 |
| 模型服务 | `foretoken:model_server_requests_waiting:sum` | 调度器中等待的请求数 |
| 模型服务 | `foretoken:model_server_e2e_request_latency_seconds:quantile5m` | 完整 model-server 请求/生成延迟的 `p50`、`p90` 和 `p99` 序列 |
| 模型服务 | `foretoken:model_server_time_to_first_token_seconds:quantile5m` | TTFT 的 `p50`、`p90` 和 `p99` 序列 |
| 模型服务 | `foretoken:model_server_time_per_output_token_seconds:quantile5m` | TPOT 的 `p50`、`p90` 和 `p99` 序列 |
| Cache | `foretoken:model_server_kv_cache_usage_ratio:max` | 最高引擎内 KV Cache 使用率 |
| Cache | `foretoken:model_server_prefix_cache_hit_ratio:rate5m` | 本地或外部 Prefix Cache 的 Token 命中率 |
| Cache | `foretoken:model_server_runtime_cache_available_bytes:min` | RuntimeCache 上报的最低可用空间 |
| Cache | `foretoken:model_server_runtime_cache_usage_ratio:max` | RuntimeCache 上报的最高文件系统使用率 |
| Cache | `foretoken:model_server_runtime_cache_observation_success:min` | 是否能够检查所有上报的 RuntimeCache 挂载点 |
| Cache | `foretoken:model_server_runtime_cache_temporary:max` | 是否有 model-server 正在使用 Pod 临时缓存 |
| 加速器 | `foretoken:accelerator_gpu_utilization_ratio` | NVIDIA 或沐曦设备利用率 |
| 加速器 | `foretoken:accelerator_gpu_memory_usage_ratio` | NVIDIA 或沐曦设备显存使用率 |

记录规则保留 namespace、Frontend 服务、模型组、模型角色、模型名称和可选的 Prefill/Decode pipeline scope。计数器会先计算可处理重置的五分钟速率，再执行聚合。Frontend HTTP 时延在 handler 返回响应时记录；对于 SSE，这表示响应开始延迟，model-server 的 E2E 规则从统一的 Frontend 到达时间计量至生成完成，不包含向客户端发送完毕的时间。Dashboard 汇总时展示各组分位数的最大值，而不是将各组合并后重新计算的分位数。原始后端指标的名称、单位和标签以 `/metrics` 中的 `HELP` 和 `TYPE` 元数据为准。

流式响应可能先以 `2xx` 开始、后续再失败，因此 `foretoken:frontend_http_response_start_5xx_ratio:rate5m` 不能作为推理成功率 SLO。

## 定位问题

[告警](alerting_zh.md)检查持续异常的信号，并通过 Alertmanager 发送通知。它需要单独启用，阈值和通知渠道在相应示例配置中选择。

定位单个慢请求时，使用[分布式追踪](tracing_zh.md)。

## 停止采集

删除全部 Foretoken 服务后，`foretoken uninstall` 会删除由 CLI 管理的 Prometheus 和 DCGM Exporter release。复用的 Prometheus、DCGM Exporter 和 mxExporter 保持不变。
