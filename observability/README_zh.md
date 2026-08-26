<!--
SPDX-License-Identifier: Apache-2.0
SPDX-FileCopyrightText: Copyright contributors to the Foretoken project
-->

# 可观测性

[English](README.md) | 简体中文

Foretoken 的 Frontend 和模型服务都提供 `/metrics`，用于查看请求、排队、路由、推理后端和缓存等运行状态。正式部署建议接入 Prometheus。

## 选择部署方式

### 推荐部署

如果集群还没有监控系统，可以安装 kube-prometheus-stack。仓库提供的 values 会让 Prometheus 只选择 Foretoken 创建的 ServiceMonitor：

```bash
helm repo add prometheus-community \
  https://prometheus-community.github.io/helm-charts
helm repo update

kubectl create namespace monitoring \
  --dry-run=client -o yaml | kubectl apply -f -

kubectl label namespace monitoring \
  inference.foretoken.io/metrics-scraper=true \
  --overwrite

helm upgrade --install kube-prometheus-stack \
  prometheus-community/kube-prometheus-stack \
  --namespace monitoring \
  --values deploy/observability/kube-prometheus-stack-values.yaml \
  --wait \
  --debug
```

示例 values 默认从 `foretoken-platform` namespace 查找 Foretoken ServiceMonitor。平台使用其他 namespace 时，复制该文件并修改 `serviceMonitorNamespaceSelector`。

安装 Foretoken 时启用 ServiceMonitor：

```yaml
observability:
  serviceMonitor:
    enabled: true
```

集群已经运行 Prometheus Operator 时，不需要重复安装 kube-prometheus-stack。只需确保：

- Prometheus 会选择 `app.kubernetes.io/name=foretoken-control-plane`；
- Prometheus 会从 Foretoken control-plane namespace 查找 ServiceMonitor；
- 运行 Prometheus 的 namespace 带有 `inference.foretoken.io/metrics-scraper=true` label。

这些是平台一次性配置，不需要为每个模型服务重复填写。

### 最小部署

本地开发、Kind 验证或不需要集中监控时，不启用 ServiceMonitor：

```yaml
observability:
  serviceMonitor:
    enabled: false
```

Frontend 和 model-server 仍提供 `/metrics`，Foretoken 的路由和自动扩缩容也继续使用内部 JSON 快照；只是 Helm Chart 不会创建 Prometheus 服务发现。

## 可选抓取参数

ServiceMonitor 默认继承平台 Prometheus 的抓取间隔和超时。只有需要单独覆盖时才设置：

```yaml
observability:
  serviceMonitor:
    enabled: true
    interval: 30s
    scrapeTimeout: 10s
```

## 验证接入

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
