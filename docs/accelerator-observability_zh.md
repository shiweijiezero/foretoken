<!-- SPDX-License-Identifier: Apache-2.0 -->
<!-- SPDX-FileCopyrightText: Copyright contributors to the Foretoken project -->

# 加速器可观测性

[English](accelerator-observability.md) | 简体中文

本文介绍如何在 Foretoken Kubernetes 集群中采集加速器硬件指标。Foretoken 复用厂商提供的 exporter，并将硬件指标与推理后端指标分开。当前版本仅提供 NVIDIA DCGM 适配器。

## 测量边界

```text
加速器硬件
  → 厂商 exporter
  → ServiceMonitor
  → Prometheus
  → 厂商 Dashboard

推理请求
  → Frontend /metrics
  → model-server /metrics
  → Prometheus
  → Foretoken 服务 Dashboard
```

加速器 exporter 负责利用率、设备内存、功耗、温度、互连和硬件错误指标。Frontend 与 model-server endpoint 负责请求、路由、scheduler、cache 和推理后端原生指标。Foretoken 不会把硬件指标复制到 model-server `/metrics`。

## 已支持的适配器

| 加速器 | Exporter | 仓库资源 | Dashboard |
| --- | --- | --- | --- |
| NVIDIA GPU | NVIDIA DCGM Exporter | `deploy/observability/accelerators/nvidia-dcgm` | `Foretoken NVIDIA GPU 运行状态` |

请按照 [NVIDIA DCGM 适配器指南](accelerators/nvidia-dcgm_zh.md)安装或复用该 exporter。其他厂商需要单独实现并验证适配器；表中没有列出的加速器不代表已经受支持。

## 适配器契约

每个适配器保留 exporter 实际发出的厂商原生 Prometheus 指标。Foretoken 和 ServiceMonitor 不会再设置 metric allowlist，也不会重命名 metric family。Collector 选择和所需权限由适配器负责，并且必须写入文档。ServiceMonitor 添加以下 target label：

| Label | 含义 | NVIDIA DCGM 的值 |
| --- | --- | --- |
| `foretoken_observability_source` | 通用硬件来源 | `accelerator` |
| `foretoken_accelerator_vendor` | 加速器厂商 | `nvidia` |
| `foretoken_accelerator_exporter` | Exporter 实现 | `dcgm` |

Prometheus 从 `accelerator-monitoring` namespace 发现仓库提供的适配器。该 namespace 是共享的接入边界，不表示不同厂商使用相同的 exporter、原始指标名、单位或设备 label。厂商 Dashboard 查询自身原生 metric family，并包含上面的三个 target label。

当前版本不提供跨厂商的归一化 metric family 或通用硬件 Dashboard。NVIDIA Dashboard 直接查询 DCGM 原生指标。其他加速器厂商目前尚未受支持，需要单独实现适配器。

## 安装监控基础设施

先按照[可观测性](../observability/README_zh.md)配置 Prometheus Operator 和 Grafana。仓库提供的 kube-prometheus-stack values 只从三个 namespace 发现 ServiceMonitor：

- `monitoring`：监控组件；
- `foretoken-platform`：Foretoken 自身的 monitor；
- `accelerator-monitoring`：加速器适配器。

可信的 Prometheus namespace 必须带有以下 label，Foretoken 和适配器的 NetworkPolicy 才允许抓取指标：

```bash
kubectl label namespace monitoring \
  inference.foretoken.io/metrics-scraper=true \
  --overwrite
```

Prometheus 需要发现额外的 ServiceMonitor namespace 时，请复制 values 文件并只扩展 namespace allowlist。如果要把仓库提供的监控组件或适配器移动到其他 namespace，还需要同步修改 Helm 命令和 namespace-scoped manifest。不要为了省事启用 cluster-wide ServiceMonitor discovery。

## 验证适配器

查看适配器 ServiceMonitor：

```bash
kubectl get servicemonitor --namespace accelerator-monitoring
```

在 Prometheus 中查询所有加速器 target：

```promql
up{foretoken_observability_source="accelerator"}
```

每个已安装的适配器都应返回 `1`，并包含自己的 `foretoken_accelerator_vendor` 和 `foretoken_accelerator_exporter` label。然后打开对应的 Grafana Dashboard，把设备清单与 node 上的厂商工具进行核对。

缺少原生 metric family 不等于数值为零。硬件型号、driver、firmware、exporter 版本和启用的 collector field 都可能影响实际存在的 series。

## 新增加速器适配器

一个适配器应当包含：

1. 厂商支持的 exporter 部署方式，或者复用已有 operator 的接入说明；
2. 使用上述三个 target label 的 ServiceMonitor；
3. 仅允许带有 `inference.foretoken.io/metrics-scraper=true` label 的 namespace 抓取指标的 NetworkPolicy；
4. 保留稳定设备身份的厂商专用 Dashboard；
5. 中英文安装、验证、排障和清理说明；
6. CI 中的 manifest 渲染和 Dashboard 查询检查。

厂商生命周期、安全权限、CRD 和原生指标语义应留在适配器内部。不要把加速器 exporter 参数加入 Foretoken 推理 chart，也不要通过 model-server 重新导出硬件指标。

## 相关文档

- [可观测性](../observability/README_zh.md)
- [NVIDIA DCGM 适配器](accelerators/nvidia-dcgm_zh.md)
- [使用 k3d 部署 Foretoken](k3d-deployment_zh.md)
