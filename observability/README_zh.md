<!--
SPDX-License-Identifier: Apache-2.0
SPDX-FileCopyrightText: Copyright contributors to the Foretoken project
-->

# 可观测性

[English](README.md) | 简体中文

可观测性用于判断服务是否健康、请求为什么变慢、容量是否充足，以及问题发生在哪一层。Foretoken 的可观测性包括持续监控、告警和按需性能诊断。

- **持续监控**：Prometheus 采集 Frontend、model-server、GPU 和 Kubernetes 指标，Grafana 用于查询和展示。
- **告警**：Prometheus 根据告警规则判断异常，Alertmanager 负责分组、抑制和发送通知。
- **Profiling**：PyTorch Profiler、Nsight Systems 和 Nsight Compute 用于按需分析算子、kernel、通信和 CPU/GPU 瓶颈。

持续监控和告警依赖集群中的 Prometheus Operator。kube-prometheus-stack 是一种可选安装方式；集群已有 Prometheus Operator 时可以直接复用。

## 准备 Prometheus

如果集群已经运行 Prometheus Operator，跳过安装步骤，继续配置 Foretoken 指标采集。

如果集群尚未安装监控系统，可以使用仓库提供的 values 安装 kube-prometheus-stack：

```bash
helm repo add prometheus-community \
  https://prometheus-community.github.io/helm-charts
helm repo update

kubectl create namespace monitoring \
  --dry-run=client -o yaml | kubectl apply -f -

helm upgrade --install kube-prometheus-stack \
  prometheus-community/kube-prometheus-stack \
  --namespace monitoring \
  --values deploy/observability/kube-prometheus-stack-values.yaml \
  --wait \
  --debug
```

示例 values 默认从 `foretoken-platform` namespace 查找 Foretoken ServiceMonitor。平台使用其他 namespace 时，复制该文件并修改 `serviceMonitorNamespaceSelector`。

为运行 Prometheus 的 namespace 添加可信标记。每个集群只需设置一次：

```bash
kubectl label namespace monitoring \
  inference.foretoken.io/metrics-scraper=true \
  --overwrite
```

该标记允许这个 namespace 访问 model-server 的内部 HTTP 端口。NetworkPolicy 无法只放行其中的 `/metrics`，因此被标记的 namespace 中只能运行可信监控服务。

## 启用 Foretoken 指标采集

在 Foretoken values 中启用 ServiceMonitor：

```yaml
observability:
  serviceMonitor:
    enabled: true
```

Chart 创建的 ServiceMonitor 会跨 workload namespace 查找带 Foretoken label 的 Frontend 和 model-server Service。平台 Prometheus 需要：

- 在 Foretoken control-plane namespace 中查找 ServiceMonitor；
- 选择带 `app.kubernetes.io/name=foretoken-control-plane` label 的 ServiceMonitor。

仓库提供的 kube-prometheus-stack values 已包含这两项配置。使用现有 Prometheus 时，由平台管理员一次性配置，不需要为每个模型服务重复填写。

ServiceMonitor 默认继承 Prometheus 的抓取间隔和超时。只有需要单独覆盖时才设置：

```yaml
observability:
  serviceMonitor:
    enabled: true
    interval: 30s
    scrapeTimeout: 10s
```

## 验证指标采集

确认 Foretoken ServiceMonitor 已创建：

```bash
kubectl get servicemonitor -A \
  -l app.kubernetes.io/name=foretoken-control-plane \
  -o wide
```

转发 Prometheus 服务：

```bash
kubectl --namespace monitoring port-forward \
  service/kube-prometheus-stack-prometheus 9090:9090
```

在另一个终端查看 Foretoken targets：

```bash
curl -sS 'http://127.0.0.1:9090/api/v1/targets?state=active' \
  | jq -r '.data.activeTargets[]
      | select((.scrapePool // "") | test("control-plane-(frontend|model-server)/"))
      | [.labels.job, .labels.namespace, .labels.pod, .health, .lastError]
      | @tsv'
```

Target 为 `UP` 表示 Prometheus 能够访问 `/metrics`，不表示模型已经可以接收推理请求。Pod 失败或重启时，对应 target 也无法正常采集。

## 指标来源

| 组件 | 入口 | 内容 |
| --- | --- | --- |
| Frontend | `GET /metrics` | HTTP 请求、准入队列、路由及其他 Frontend 运行指标 |
| model-server | `GET /metrics` | 当前推理后端提供的完整原生指标 |
| model-server | `GET /v1/internal/telemetry` | Foretoken 路由和自动扩缩容直接读取的版本化 JSON 快照 |

Prometheus 只负责观测，不参与 Foretoken 的路由或自动扩缩容控制环。对于当前 vLLM adapter，指标名称、单位和 label 以 `/metrics` 响应中的 `HELP` 和 `TYPE` 为准。

GPU 和 Kubernetes 状态来自集群已有的标准数据源：

| 信号 | 数据源 |
| --- | --- |
| NVIDIA GPU 利用率、显存、功耗、温度、互联和硬件错误 | DCGM Exporter |
| 其他 accelerator 硬件 | 对应厂商的 accelerator exporter |
| Container CPU、内存、文件系统和网络 | kubelet/cAdvisor |
| Kubernetes object 状态 | kube-state-metrics 或对应 controller |

## 告警和 Profiling

告警规则由 Prometheus 评估，并交给 Alertmanager 发送通知。告警应使用 `/metrics` 中持续采集的时间序列，不读取 Foretoken 内部 telemetry 接口。

Profiling 用于一次具体实验或故障分析，不属于持续指标采集。PyTorch Profiler 适合分析模型执行与算子时间；Nsight Systems 适合分析进程、kernel 和通信时间线；Nsight Compute 适合深入分析单个 GPU kernel。Profiling 结果应与对应 Benchmark 的模型、并发、硬件和运行参数一起保存。
