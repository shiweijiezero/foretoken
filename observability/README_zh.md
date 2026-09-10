<!--
SPDX-License-Identifier: Apache-2.0
SPDX-FileCopyrightText: Copyright contributors to the Foretoken project
-->

# 可观测性

[English](README.md) | 简体中文

Foretoken 会为服务和加速器指标安装 Prometheus 采集、记录规则以及可选告警规则。Alertmanager 路由和通知继续由平台团队负责。

## 安装采集

```bash
foretoken install
```

CLI 会发现采集路径，并在修改集群前打印安装计划。

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

# 使用 CLI 管理的 Prometheus 时，在本机打开 Prometheus UI
kubectl port-forward \
  --namespace foretoken-platform \
  service/foretoken-prometheus-kube-prometheus \
  9090:9090
```

打开 <http://127.0.0.1:9090/targets>，确认 Foretoken target 为 `UP`；再打开 <http://127.0.0.1:9090/rules>，确认 `foretoken.recording` 和 `foretoken.alerting` 均已加载。复用已有 Prometheus 时，通过平台原有的访问方式执行相同检查。

以下查询可以查看 Frontend 请求量：

```promql
sum(foretoken:frontend_http_response_starts:rate5m)
```

## 打开 Grafana Dashboard

由 CLI 管理的 kube-prometheus-stack 会自动加载 **Foretoken System Overview**。获取自动生成的管理员凭据，并在本机打开 Grafana：

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

kubectl port-forward \
  --namespace foretoken-platform \
  service/foretoken-prometheus-grafana \
  3000:80
```

打开 <http://127.0.0.1:3000>，进入 **Dashboards** 并选择 **Foretoken System Overview**。这一套 Dashboard 按请求链路依次展示 Frontend 流量和准入、model-server 延迟与吞吐、调度状态、KV Cache 与 RuntimeCache、加速器利用率和服务容器资源。页面提供命名空间、Frontend 服务、模型组、模型角色和模型筛选。

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
| Frontend | `foretoken:frontend_http_request_duration_seconds:quantile5m` | 请求延迟，通过 `quantile` 标签区分 `p50`、`p90` 和 `p99` |
| Frontend | `foretoken:frontend_upstream_queued_requests:sum` | 按扩缩容目标统计的准入等待请求数 |
| Frontend | `foretoken:frontend_kv_index_source_health_ratio:min` | Frontend 副本中最低的 KV 事件源健康比例 |
| 模型服务 | `foretoken:model_server_up:sum` | 正在上报的 model-server target 数量 |
| 模型服务 | `foretoken:model_server_completed_requests:rate5m` | 按结束原因统计的每秒完成请求数 |
| 模型服务 | `foretoken:model_server_prompt_tokens:rate5m` | 每秒处理的输入 Token 数 |
| 模型服务 | `foretoken:model_server_generation_tokens:rate5m` | 每秒生成的输出 Token 数 |
| 模型服务 | `foretoken:model_server_requests_running:sum` | 当前运行中的请求数 |
| 模型服务 | `foretoken:model_server_requests_waiting:sum` | 调度器中等待的请求数 |
| 模型服务 | `foretoken:model_server_e2e_request_latency_seconds:quantile5m` | E2E 延迟的 `p50`、`p90` 和 `p99` 序列 |
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

记录规则保留 namespace、Frontend 服务、模型组、模型角色、模型名称和可选的 Prefill/Decode pipeline scope。计数器会先计算可处理重置的五分钟速率，再执行聚合。原始后端指标的名称、单位和标签以 `/metrics` 中的 `HELP` 和 `TYPE` 元数据为准。

流式响应可能先以 `2xx` 开始、后续再失败，因此 `foretoken:frontend_http_response_start_5xx_ratio:rate5m` 不能作为推理成功率 SLO。

## 告警、Lark 与性能剖析

启用可观测性后，Chart 会和记录规则一起渲染告警规则。查看实现的最短路径如下：

1. 在 `deploy/charts/foretoken/values.yaml` 中将 `observability.mode` 设为 `enabled`（或使用 `auto`）。
2. 在同一个文件中查看阈值和语言选项。
3. 在 `deploy/charts/foretoken/files/alerting-rules.yaml` 查看规则定义，在 `deploy/charts/foretoken/templates/alertingrule.yaml` 查看 Chart 如何渲染它。
4. 用[告警排障手册](runbooks/alerts_zh.md)执行排查，用 [Lark 通知集成](integrations/lark/README_zh.md)查看路由和消息格式。

Lark 集成支持 `zh`、`en` 和 `bilingual` 三种消息语言。一次部署的共享告警只选择一种语言；同一条分组消息不能针对不同接收人分别翻译。

例如，保留默认阈值并选择英文消息：

```yaml
observability:
  mode: enabled
  alerts:
    language: en
    thresholds:
      acceleratorMemoryUsageRatio: 0.90
      nvidiaTemperatureCelsius: 80
```

Alertmanager 负责通知接收方、分组和路由。Foretoken 提供告警表达式和默认阈值；如果设备或 workload 需要不同限制，可以通过 Chart values 覆盖。

在受控负载中短时间采集 Torch trace，见[benchmark profiling](../benchmarks/README_zh.md#按需-profiling)。命令负责提交和收取结果，model-server 负责窗口计时。Profiling 独立于监控和告警配置；所需引擎修复及当前验证限制见 benchmark 指南。

## 停止采集

删除全部 Foretoken 服务后，`foretoken uninstall` 会删除由 CLI 管理的 Prometheus 和 DCGM Exporter release。复用的 Prometheus、DCGM Exporter 和 mxExporter 保持不变。
