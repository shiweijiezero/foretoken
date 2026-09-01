<!--
SPDX-License-Identifier: Apache-2.0
SPDX-FileCopyrightText: Copyright contributors to the Foretoken project
-->

# 可观测性

[English](README.md) | 简体中文

Foretoken 通过指标、告警和按需性能剖析展示服务健康状态、请求延迟、队列压力、可用容量和硬件利用率。

## 采集指标

安装 Foretoken 时会配置 Prometheus 采集和记录规则：

```bash
foretoken install
```

CLI 会复用集群中唯一兼容的 Prometheus；如果没有，则安装由 CLI 管理的 kube-prometheus-stack。

只有自动发现得到多个兼容实例时，才需要显式选择 Prometheus。先允许该 Prometheus 所在的命名空间采集 Foretoken 指标，再指定实例：

```bash
kubectl label namespace monitoring \
  inference.foretoken.io/metrics-scraper=true \
  --overwrite

foretoken install --prometheus monitoring/prometheus
```

该命名空间标签继续由 Prometheus 所属平台管理。不再采集 Foretoken 指标时，也应通过该平台移除标签。

## 验证采集状态

查看 Foretoken 的 ServiceMonitor 和记录规则：

```bash
kubectl get servicemonitor,prometheusrule -A \
  -l app.kubernetes.io/name=foretoken-control-plane
```

使用 CLI 管理的 Prometheus 时，将服务转发到本机：

```bash
kubectl port-forward \
  --namespace foretoken-platform \
  service/foretoken-prometheus-kube-prometheus \
  9090:9090
```

打开 <http://127.0.0.1:9090/targets>，确认 Foretoken target 为 `UP`；再打开 <http://127.0.0.1:9090/rules>，确认 `foretoken.recording` 已加载。复用已有 Prometheus 时，通过平台原有的访问方式执行相同检查。

## 指标来源

| 来源 | 内容 |
| --- | --- |
| Frontend `/metrics` | HTTP 请求、准入队列、路由和前端运行状态 |
| model-server `/metrics` | 当前推理后端提供的原生指标 |
| DCGM Exporter | NVIDIA GPU 利用率、显存、功耗、温度和硬件错误 |
| kubelet/cAdvisor | 容器 CPU、内存、文件系统和网络 |
| kube-state-metrics | Kubernetes 资源状态 |

model-server 指标的名称、单位和标签以 `/metrics` 中的 `HELP` 和 `TYPE` 元数据为准。

## 记录规则

Foretoken 的 `PrometheusRule` 在 Frontend 和 model-server 指标之上提供稳定、低基数的查询：

| 记录规则 | 含义 |
| --- | --- |
| `foretoken:frontend_http_response_starts:rate5m` | 按 method、handler 和状态类别区分的 Frontend HTTP 响应开始速率 |
| `foretoken:frontend_http_response_start_5xx_ratio:rate5m` | Frontend 响应开始时的 5xx 比例；不是推理失败率 |
| `foretoken:model_server_prompt_tokens:rate5m` | 每秒处理的输入 token 数 |
| `foretoken:model_server_generation_tokens:rate5m` | 每秒生成的输出 token 数 |
| `foretoken:model_server_requests_running:sum` | 当前运行中的请求数 |
| `foretoken:model_server_requests_waiting:sum` | vLLM 调度器中当前等待的请求数 |
| `foretoken:model_server_kv_cache_usage_ratio:max` | 最高 KV Cache 使用比例 |

记录规则保留 namespace、Frontend 服务、模型组与角色、模型名称和可选的 Prefill/Decode pipeline scope。计数器会先计算可处理重置的五分钟速率，再执行聚合。

Frontend 在响应开始时记录 HTTP 状态。后续流式传输失败时，状态仍可能是 `2xx`，因此响应开始时的 5xx 比例不能作为推理成功率 SLO。

## 告警

Prometheus 评估服务不可用、错误率升高、队列持续积压、延迟异常和容量耗尽等持续状态。通知接收方、分组和路由由 Alertmanager 管理。

## 性能剖析

性能剖析用于调查可复现的实验或故障，不用于持续监控：

- PyTorch Profiler 分析模型执行、算子时间和内存；
- Nsight Systems 分析进程、kernel 和通信时间线；
- Nsight Compute 分析单个 GPU kernel。

性能剖析会影响推理性能。采集时使用小规模工作负载，并将模型、并发、硬件和运行参数与结果一起记录。

## 停止采集

删除全部 Foretoken 服务后，`foretoken uninstall` 会删除由 CLI 管理的 Prometheus 发布实例；复用的 Prometheus 保持不变。
