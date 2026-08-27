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

集群需要 Prometheus Operator。已经有 Prometheus Operator 时直接复用；没有时，可以使用仓库提供的 values 配置文件安装 kube-prometheus-stack：

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

允许 monitoring 命名空间访问 model-server 指标端口：

```bash
kubectl label namespace monitoring \
  inference.foretoken.io/metrics-scraper=true \
  --overwrite
```

安装 Foretoken 时启用 ServiceMonitor：

```yaml
observability:
  serviceMonitor:
    enabled: true
```

ServiceMonitor 默认继承 Prometheus 的抓取间隔和超时。只有需要单独覆盖时才设置 `interval` 或 `scrapeTimeout`。

仓库提供的 kube-prometheus-stack values 配置文件默认从 `foretoken-platform` 命名空间查找 Foretoken ServiceMonitor。平台使用其他命名空间时，请复制该文件并修改 `serviceMonitorNamespaceSelector`。

## 确认采集已启用

```bash
kubectl get pods --namespace monitoring

kubectl get servicemonitor -A \
  -l app.kubernetes.io/name=foretoken-control-plane
```

Prometheus、Grafana 和 Alertmanager Pod 应处于 `Running`，并且启用的 Foretoken 组件应有对应的 ServiceMonitor。指标没有出现时，再检查 ServiceMonitor、Service 标签、命名端口和 Pod 的 `/metrics`，这些属于排障步骤，不是正常使用流程。

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
