<!--
SPDX-License-Identifier: Apache-2.0
SPDX-FileCopyrightText: Copyright contributors to the Foretoken project
-->

# 可观测性

[English](README.md) | 简体中文

可观测性帮助运维人员了解服务是否健康、请求变慢的原因、容量是否充足，以及问题发生在哪一层。Foretoken 通过指标、告警和按需性能剖析支持这些工作。

- **指标与仪表盘**：Prometheus 持续采集运行指标，Grafana 用于查询和展示。
- **告警**：Prometheus 判断持续异常，Alertmanager 负责通知、分组和静默。
- **性能剖析**：PyTorch Profiler 和 Nsight 用于在复现问题时分析 CPU、GPU、kernel 和通信瓶颈。

## 启用指标采集

Frontend 和 model-server 无需 Prometheus 即可提供 `/metrics`。如需采集这些指标并计算记录规则，可以复用已有 Prometheus Operator，也可以安装 kube-prometheus-stack：

```bash
helm repo add prometheus-community \
  https://prometheus-community.github.io/helm-charts
helm repo update

helm upgrade --install kube-prometheus-stack \
  prometheus-community/kube-prometheus-stack \
  --namespace monitoring \
  --create-namespace \
  --values deploy/observability/kube-prometheus-stack-values.yaml \
  --wait
```

为实际运行 Prometheus Pod 的命名空间添加标签，使其可以访问 model-server 指标：

```bash
PROMETHEUS_NAMESPACE=monitoring
kubectl label namespace "${PROMETHEUS_NAMESPACE}" \
  inference.foretoken.io/metrics-scraper=true \
  --overwrite
```

Operator CRD 建立后，在线安装 Foretoken 时，默认的 `auto` 模式会创建 ServiceMonitor 和 PrometheusRule：

```bash
foretoken install
```

如果 Foretoken 平台由 CLI 管理，请将接入配置写入 values 文件，然后再次运行 `foretoken install`：

```yaml
observability:
  mode: enabled
  additionalLabels:
    release: your-prometheus-release
```

```bash
foretoken install --values foretoken-observability-values.yaml
```

在同一文件中使用 `disabled` 可删除 Chart 管理的监控资源。原本通过 Helm 直接安装的发布实例继续使用原有 Helm 生命周期，CLI 不会自动接管。

`auto` 只在集群同时提供 `ServiceMonitor` 和 `PrometheusRule` API 时创建两种资源。离线渲染和 GitOps 应使用 `enabled`，并先安装 Operator CRD。只有需要覆盖 Prometheus 默认值时才设置 `interval` 或 `scrapeTimeout`。

仓库提供的 kube-prometheus-stack values 配置文件默认从 `foretoken-platform` 命名空间选择 Foretoken 资源。复用其他 Prometheus 安装时，其 ServiceMonitor 与规则命名空间选择器必须包含 Foretoken 发布实例所在命名空间。只有对象选择器要求平台自定义标签时才设置 `additionalLabels`。

## 确认采集已启用

先确认 Kubernetes 中存在预期资源：

```bash
kubectl get servicemonitor,prometheusrule -A \
  -l app.kubernetes.io/name=foretoken-control-plane
```

使用仓库提供的 kube-prometheus-stack 安装时，将 Prometheus 转发到本机：

```bash
kubectl port-forward \
  --namespace monitoring \
  service/kube-prometheus-stack-prometheus \
  9090:9090
```

在 Prometheus 的 <http://127.0.0.1:9090/targets> 确认 Foretoken target 为 `UP`，并在 <http://127.0.0.1:9090/rules> 确认 `foretoken.recording` 已加载。服务收到请求后，可以查询记录指标：

```bash
curl --get http://127.0.0.1:9090/api/v1/query \
  --data-urlencode 'query=foretoken:model_server_requests_running:sum'
```

仅看到 Kubernetes 对象并不能证明 Prometheus 已选择 Monitor 或加载规则。复用平台已有 Prometheus 时，使用该平台原有的访问方式执行相同的 target、rule 和查询检查。

## 关闭或移除接入

设置 `observability.mode=disabled` 或卸载 Foretoken，只会删除 Foretoken 发布实例管理的 ServiceMonitor 和 PrometheusRule，不会卸载 Prometheus、Prometheus Operator、Grafana 或 Alertmanager。没有其他 Foretoken workload 需要该 Prometheus 安装时，可以移除命名空间标签：

```bash
kubectl label namespace monitoring \
  inference.foretoken.io/metrics-scraper-
```

Prometheus 运行在其他命名空间时，替换命令中的 `monitoring`。

## 记录规则

可选的 PrometheusRule 在 Frontend 和 model-server 原始指标之上提供稳定、低基数的查询：

| 记录规则 | 含义 |
| --- | --- |
| `foretoken:frontend_http_response_starts:rate5m` | 按 method、handler 和状态类别区分的 Frontend HTTP 响应开始速率 |
| `foretoken:frontend_http_response_start_5xx_ratio:rate5m` | Frontend 响应开始时的 5xx 比例；不是推理失败率 |
| `foretoken:model_server_prompt_tokens:rate5m` | 每秒处理的输入 token 数 |
| `foretoken:model_server_generation_tokens:rate5m` | 每秒生成的输出 token 数 |
| `foretoken:model_server_requests_running:sum` | 当前运行中的请求数 |
| `foretoken:model_server_requests_waiting:sum` | vLLM 调度器中当前等待的请求数 |
| `foretoken:model_server_kv_cache_usage_ratio:max` | 最高 KV Cache 使用比例 |

规则保留 namespace、Frontend 服务、模型组与角色、模型名称和可选的 Prefill/Decode pipeline scope。计数器先按原始序列计算可处理重置的五分钟速率，再执行聚合。model-server 样本缺失时结果保持缺失，实际观测到的 gauge 零值仍保持为零。

Frontend HTTP 状态在响应开始时记录。后续流式传输失败时状态仍可能是 `2xx`，因此响应开始时的 5xx 比例不能作为面向用户的推理成功率 SLO。

## 指标来源

| 来源 | 内容 |
| --- | --- |
| Frontend `/metrics` | HTTP 请求、准入队列、路由及前端服务运行状态 |
| model-server `/metrics` | 当前推理后端提供的完整原生指标 |
| DCGM Exporter | NVIDIA GPU 利用率、显存、功耗、温度和硬件错误 |
| kubelet/cAdvisor | 容器 CPU、内存、文件系统和网络 |
| kube-state-metrics | Kubernetes 对象状态 |

model-server 不会重命名或过滤推理后端的原生指标。对于当前 vLLM 适配器，指标名称、单位和标签以 `/metrics` 响应中的 `HELP` 和 `TYPE` 为准。

Prometheus 只负责观测，不参与 Foretoken 的路由或自动扩缩容控制环。路由和自动扩缩容仍直接读取 model-server 中带版本标识的内部快照。

## 告警

告警规则由 Prometheus 评估，并交给 Alertmanager 发送通知。告警应关注需要处理的持续状态，例如服务不可用、错误率升高、队列持续积压、延迟异常和容量耗尽。通知接收方式和路由由平台 Alertmanager 统一配置。

## 性能剖析

性能剖析用于分析某次具体实验或故障，不是持续监控，也不应默认开启：

- PyTorch Profiler：分析模型执行、算子时间和内存；
- Nsight Systems：分析进程、kernel 和通信时间线；
- Nsight Compute：深入分析单个 GPU kernel。

性能剖析会影响推理性能。采集时只发送少量可复现请求，并将结果与模型、并发、硬件和运行参数一起保存。
