<!--
SPDX-License-Identifier: Apache-2.0
SPDX-FileCopyrightText: Copyright contributors to the Foretoken project
-->

# 可观测性

[English](README.md) | 简体中文

Foretoken 会为服务和加速器指标安装 Prometheus 采集与记录规则，但不会安装 Foretoken 告警规则。告警阈值、Alertmanager 路由和通知继续由平台团队负责。

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

打开 <http://127.0.0.1:9090/targets>，确认 Foretoken target 为 `UP`；再打开 <http://127.0.0.1:9090/rules>，确认 `foretoken.recording` 已加载。复用已有 Prometheus 时，通过其平台提供的访问方式执行相同检查。

以下查询可以查看 Frontend 请求量：

```promql
sum(foretoken:frontend_http_response_starts:rate5m)
```

## 指标与记录规则

| 来源 | 内容 |
| --- | --- |
| Frontend `/metrics` | HTTP 请求、准入队列、路由和运行状态 |
| model-server `/metrics` | 当前推理后端提供的原生指标 |
| DCGM Exporter | NVIDIA 利用率、显存、功耗、温度和 XID 错误 |
| mxExporter | 沐曦利用率和显存指标 |
| kubelet/cAdvisor | 容器 CPU、内存、文件系统和网络 |
| kube-state-metrics | Kubernetes 资源状态 |

以下稳定记录规则目前由 Frontend 指标和 vLLM model-server 指标族生成，不是其他推理后端的统一指标契约。

| 记录规则 | 含义 |
| --- | --- |
| `foretoken:frontend_http_response_starts:rate5m` | Frontend 每秒开始的 HTTP 响应数 |
| `foretoken:frontend_http_response_start_5xx_ratio:rate5m` | 响应开始时的 5xx 比例，不是推理失败率 |
| `foretoken:model_server_prompt_tokens:rate5m` | vLLM 每秒处理的输入 token 数 |
| `foretoken:model_server_generation_tokens:rate5m` | vLLM 每秒生成的输出 token 数 |
| `foretoken:model_server_requests_running:sum` | vLLM 当前运行中的请求数 |
| `foretoken:model_server_requests_waiting:sum` | vLLM 调度器中等待的请求数 |
| `foretoken:model_server_kv_cache_usage_ratio:max` | 最高 vLLM KV Cache 使用比例 |

记录规则保留 namespace、Frontend 服务、模型组、模型角色、模型名称和可选的 Prefill/Decode pipeline scope。计数器会先计算可处理重置的五分钟速率，再执行聚合。原始后端指标的名称、单位和标签以 `/metrics` 中的 `HELP` 和 `TYPE` 元数据为准。

流式响应可能先以 `2xx` 开始、后续再失败，因此 `foretoken:frontend_http_response_start_5xx_ratio:rate5m` 不能作为推理成功率 SLO。

## 告警与性能剖析

Foretoken 当前提供指标和记录规则，不提供告警规则。请在平台团队负责的 Prometheus 与 Alertmanager 配置中定义告警阈值和通知策略。

Foretoken 不管理性能剖析流程。调查可复现实验时，使用受控负载，并通过模型运行环境和硬件平台使用 PyTorch Profiler、Nsight Systems 或 Nsight Compute。性能剖析会影响服务性能，应记录模型、负载、硬件和运行参数。

## 停止采集

删除全部 Foretoken 服务后，`foretoken uninstall` 会删除由 CLI 管理的 Prometheus 和 DCGM Exporter release。复用的 Prometheus、DCGM Exporter 和 mxExporter 保持不变。
