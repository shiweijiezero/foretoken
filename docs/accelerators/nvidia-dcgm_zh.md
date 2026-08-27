<!-- SPDX-License-Identifier: Apache-2.0 -->
<!-- SPDX-FileCopyrightText: Copyright contributors to the Foretoken project -->

# NVIDIA DCGM 加速器适配器

[English](nvidia-dcgm.md) | 简体中文

本文介绍如何把 NVIDIA GPU 硬件遥测接入 Foretoken 加速器可观测性边界：安装或复用 NVIDIA DCGM Exporter，让 Prometheus 发现 exporter，并加载中文 Grafana Dashboard。

请先阅读[加速器可观测性](../accelerator-observability_zh.md)，了解跨厂商边界。DCGM 是 NVIDIA 适配器；它的 `DCGM_FI_*` 指标名和 `UUID` 设备 label 不是其他厂商共用的 schema。

## 选择部署路径

| 集群现状 | 部署路径 |
| --- | --- |
| 目标 node 已运行 GPU Operator 或 DCGM Exporter | [复用现有 exporter](#复用现有-exporter) |
| 标准 Kubernetes GPU node 尚无 exporter | [安装独立适配器](#安装独立适配器) |
| Foretoken k3d 开发集群 | 安装独立适配器，再应用 [k3d 设备 overlay](#在-k3d-中选择设备) |

不要在同一批 node 上运行两个 DCGM Exporter DaemonSet。重复 exporter 会浪费资源，也可能产生重复 series。

## 前置条件

安装适配器前，请确认：

- 每个目标 node 上的 NVIDIA driver 和 NVIDIA Container Toolkit 正常工作；
- container runtime 可以向 container 暴露 GPU；
- 已按照[可观测性](../../observability/README_zh.md)安装 Prometheus Operator 和 Grafana；
- Prometheus namespace 已添加 `inference.foretoken.io/metrics-scraper=true` label；
- 已确认其他平台组件是否提供 DCGM Exporter。

使用 k3d 时，请先完成[使用 k3d 部署 Foretoken](../k3d-deployment_zh.md)，并验证 Kubernetes 报告的 GPU capacity。

下面的命令都应从 Foretoken 仓库根目录运行。

## 安装独立适配器

仓库提供一个由适配器维护的 values 文件。所有 node 都是 NVIDIA GPU node、并且默认 runtime 可以暴露 GPU 时，无需修改该文件；混合 node 或非默认 runtime 集群应在安装前应用下文的小型 override。基础配置让 DCGM Exporter 以非 root 用户运行、移除 `SYS_ADMIN`、不预留可调度的 `nvidia.com/gpu` 资源，并使用非 profiling 的硬件 collector。

添加官方 chart 仓库：

```bash
helm repo add gpu-helm-charts \
  https://nvidia.github.io/dcgm-exporter/helm-charts
helm repo update
```

创建适配器 namespace 并应用 ingress policy：

```bash
kubectl create namespace accelerator-monitoring \
  --dry-run=client \
  --output yaml \
  | kubectl apply --filename -

kubectl apply \
  --filename deploy/observability/accelerators/nvidia-dcgm/network-policy.yaml
```

安装固定版本的 chart：

```bash
helm upgrade --install dcgm-exporter \
  gpu-helm-charts/dcgm-exporter \
  --version 4.8.2 \
  --namespace accelerator-monitoring \
  --values deploy/observability/accelerators/nvidia-dcgm/values.yaml \
  --wait
```

Chart `4.8.2` 固定了对应的 `4.5.3-4.8.2-distroless` image。修改 chart 版本前，请阅读上游 release note，并重新验证渲染后的安全上下文、collector 和 Dashboard。

即使 `--kubernetes=false` 已在运行时关闭 Pod attribution，上游 chart 仍会渲染只读的 `/var/lib/kubelet/pod-resources` hostPath。禁止该 hostPath 的集群策略可能拒绝 DaemonSet。此时应复用平台管理的 exporter，或验证与上游兼容的 chart overlay；不要只为这个 addon 放宽 Pod Security policy。

### 在 k3d 中选择设备

在标准 Kubernetes 中，DaemonSet 观测每个目标 node 上的 GPU。在嵌套的 k3d 开发集群中，需要显式传入创建 cluster 时所选同一组物理 GPU 对应的宿主机 index，并通过设备 UUID 与 NVIDIA device plugin 核对映射关系。

复制只有一个可修改值的 overlay：

```bash
cp \
  deploy/observability/accelerators/nvidia-dcgm/k3d-values.example.yaml \
  /tmp/foretoken-nvidia-dcgm-k3d-values.yaml
```

只修改副本中的 `extraEnv[0].value`。如果 cluster 使用 `GPU_INDICES=6,7` 创建，请保持：

```yaml
extraEnv:
  - name: NVIDIA_VISIBLE_DEVICES
    value: "6,7"
```

使用两个 values 文件安装或更新 exporter：

```bash
helm upgrade --install dcgm-exporter \
  gpu-helm-charts/dcgm-exporter \
  --version 4.8.2 \
  --namespace accelerator-monitoring \
  --values deploy/observability/accelerators/nvidia-dcgm/values.yaml \
  --values /tmp/foretoken-nvidia-dcgm-k3d-values.yaml \
  --wait
```

该配置为可信开发集群选择设备，不是租户隔离边界。某些 runtime 配置下，privileged k3d node 仍可能枚举其他宿主机设备。不可信 workload 应使用常规 node 或 VM 隔离。

### 可选的集群专用覆盖

请把集群专用设置放在一个很小的第二 values 文件中，不要修改适配器默认值：

| 场景 | 覆盖项 |
| --- | --- |
| CPU 与 GPU 混合 node | 使用平台维护的 label 设置 `nodeSelector`，例如 `nvidia.com/gpu.present: "true"` |
| 集群要求显式 NVIDIA runtime | 确认 RuntimeClass 存在后设置 `runtimeClassName: nvidia` |
| 加速器 node 带有 taint | 添加最小范围的匹配 `tolerations` |

不要因为示例中出现了 RuntimeClass、node label 或 toleration 就直接添加。请先检查目标集群。

## 复用现有 exporter

GPU Operator 或其他平台组件已经管理 DCGM Exporter 时，不要再安装 standalone chart。请让它的 ServiceMonitor 添加加速器适配器 label：

```yaml
relabelings:
  - targetLabel: foretoken_observability_source
    replacement: accelerator
  - targetLabel: foretoken_accelerator_vendor
    replacement: nvidia
  - targetLabel: foretoken_accelerator_exporter
    replacement: dcgm
```

ServiceMonitor 所在 namespace 必须包含在 Prometheus 的 `serviceMonitorNamespaceSelector` 中。仓库提供的 kube-prometheus-stack values 已包含 `accelerator-monitoring`；operator 使用其他 namespace 时，请复制该文件并加入对应名称。

加载 Dashboard 前，请检查一条原始 series，确认需要的 `DCGM_FI_*` metric family 存在，并且每张 GPU 的 series 都包含大写 `UUID` label。DCGM legacy namespace 模式可能使用小写 `uuid`；复用前应关闭该模式，或者调整 NVIDIA Dashboard。

仓库提供的 NetworkPolicy 选择 standalone chart 渲染的 Pod label。复用 operator 管理的 exporter 时，请检查实际 Pod label；如果 operator 没有管理访问策略，需要提供等价的 policy。

## 加载 Grafana Dashboard

把适配器 Dashboard ConfigMap 应用到 Grafana 所在 namespace：

```bash
kubectl apply \
  --namespace monitoring \
  --kustomize deploy/observability/accelerators/nvidia-dcgm/grafana
```

kube-prometheus-stack sidecar 会发现 `grafana_dashboard=1` label，并加载 `Foretoken NVIDIA GPU 运行状态`。无论 Grafana 账号使用哪种界面语言，Dashboard 文本都保持中文。

Foretoken Frontend 和 model-server 指标与该适配器互不依赖。请按照[可观测性](../../observability/README_zh.md)，通过 `observability.serviceMonitor.enabled=true` 单独启用它们的 ServiceMonitor。

## 验证适配器

### 1. Exporter 资源

```bash
kubectl rollout status \
  daemonset/dcgm-exporter \
  --namespace accelerator-monitoring \
  --timeout=5m

kubectl get daemonset,service,servicemonitor \
  --namespace accelerator-monitoring
```

DaemonSet 的 desired 和 ready 数量应与它预期监控的 node 数量一致。基础 values 会选择所有 node，因此假设集群中的 node 都有 GPU；混合集群继续操作前应确认 `nodeSelector`。

### 2. 原生指标

```bash
kubectl port-forward \
  --namespace accelerator-monitoring \
  service/dcgm-exporter \
  19400:9400
```

在另一个终端运行：

```bash
curl --fail --silent http://127.0.0.1:19400/metrics \
  | grep -E '^DCGM_FI_DEV_(GPU_TEMP|GPU_UTIL|FB_USED)\{'
```

每张 exporter 可见 GPU 至少有一条 series，说明 DCGM 能够读取设备。验证后停止 port-forward。

### 3. Prometheus target 与设备清单

适配器 target 应处于 up 状态：

```promql
min(up{
  foretoken_observability_source="accelerator",
  foretoken_accelerator_vendor="nvidia",
  foretoken_accelerator_exporter="dcgm"
})
```

结果应为 `1`。使用以下查询统计 exporter 可见设备：

```promql
count(
  group by (instance, gpu, UUID) (
    DCGM_FI_DEV_GPU_TEMP{
      foretoken_observability_source="accelerator",
      foretoken_accelerator_vendor="nvidia",
      foretoken_accelerator_exporter="dcgm"
    }
    or DCGM_FI_DEV_GPU_UTIL{
      foretoken_observability_source="accelerator",
      foretoken_accelerator_vendor="nvidia",
      foretoken_accelerator_exporter="dcgm"
    }
    or DCGM_FI_DEV_FB_USED{
      foretoken_observability_source="accelerator",
      foretoken_accelerator_vendor="nvidia",
      foretoken_accelerator_exporter="dcgm"
    }
  )
)
```

把 UUID 和数量与 node 上的 `nvidia-smi -L` 对照。Exporter 可见设备数与 Kubernetes allocatable GPU 数测量的是不同边界；只有部署有意选择同一设备集合时，两者才应相同。

### 4. Grafana

```bash
kubectl port-forward \
  --namespace monitoring \
  service/kube-prometheus-stack-grafana \
  3000:80
```

推荐的 kube-prometheus-stack release 使用用户名 `admin`。从 release Secret 读取生成的密码：

```bash
kubectl get secret kube-prometheus-stack-grafana \
  --namespace monitoring \
  --output jsonpath='{.data.admin-password}' \
  | base64 --decode
printf '\n'
```

打开 `http://127.0.0.1:3000`，搜索 `Foretoken NVIDIA GPU 运行状态`。不要把生成的密码写入文档或源代码。

## 指标范围与语义

适配器完整保留配置好的原生输出。它的 non-profiling collector 包含：

| 类别 | 示例 |
| --- | --- |
| 利用率 | GPU、memory-copy、encoder 和 decoder 利用率 |
| 内存 | Framebuffer 已用/空闲和显存温度 |
| 温度与功耗 | GPU 温度、功耗、累计能耗和限速 counter |
| 时钟与互连 | SM/显存时钟、PCIe 和 NVLink field |
| 可靠性 | XID、ECC、退役/重映射 page 和 row-remap 状态 |

Dashboard 展示当前已识别的子集，Prometheus 仍保留其他已配置的 metric family。不受硬件支持的 field 可能不存在。缺少 series 不等于数值为零，Dashboard 颜色阈值也只是视觉提示，不是告警策略。

## 排障

| 现象 | 检查项 |
| --- | --- |
| Exporter Pod 未 Ready | NVIDIA runtime、driver 兼容性、node 选择和 Pod 日志 |
| Prometheus target down | ServiceMonitor label、discovery namespace、可信 scraper namespace label 和 NetworkPolicy |
| Target up 但没有 GPU series | Exporter 可见设备、DCGM 初始化和启用的 collector field |
| Dashboard 无数据 | Prometheus datasource、三个适配器 label、原生指标名和大写 `UUID` |
| 设备 series 重复 | 是否存在多个 exporter，或多个 ServiceMonitor 抓取同一 target |

常用命令：

```bash
kubectl logs \
  --namespace accelerator-monitoring \
  daemonset/dcgm-exporter

kubectl get prometheus \
  --namespace monitoring \
  --output yaml
```

## 清理

对于独立安装，只删除本文创建的 release 和 policy：

```bash
helm uninstall dcgm-exporter \
  --namespace accelerator-monitoring

kubectl delete \
  --ignore-not-found \
  --filename deploy/observability/accelerators/nvidia-dcgm/network-policy.yaml

rm -f /tmp/foretoken-nvidia-dcgm-k3d-values.yaml
```

复用现有 exporter 时，不要执行 `helm uninstall`。请通过原 lifecycle owner 只撤销为 Foretoken 添加的三个 relabeling 或其他接入改动。

两种路径都可以在不再需要时删除 Foretoken Dashboard：

```bash
kubectl delete configmap foretoken-nvidia-gpu-dashboard \
  --namespace monitoring \
  --ignore-not-found
```

只有不再有其他加速器适配器使用时，才能删除 `accelerator-monitoring`。清理 Foretoken 时，不要卸载共享 GPU Operator、Prometheus stack 或 Prometheus Operator CRD。

## 参考资料

- [安装 NVIDIA DCGM Exporter](https://docs.nvidia.com/datacenter/dcgm/latest/installation/install-dcgm-exporter.html)
- [DCGM Exporter 指标 field、单位和 label](https://docs.nvidia.com/datacenter/dcgm/latest/reference/dcgm-exporter-metrics.html)
- [NVIDIA DCGM Exporter chart values](https://github.com/NVIDIA/dcgm-exporter/blob/4.5.3-4.8.2/deployment/values.yaml)
