<!-- SPDX-License-Identifier: Apache-2.0 -->
<!-- SPDX-FileCopyrightText: Copyright contributors to the Foretoken project -->

# 监控 NVIDIA GPU

[English](gpu-observability.md) | 简体中文

本文档为 Foretoken Kubernetes cluster 增加持续的 NVIDIA GPU 硬件监控：以 cluster addon 的形式安装 NVIDIA DCGM Exporter，由 Prometheus 发现它配置的完整输出，并加载中文 Grafana Dashboard。

GPU 采集链路与推理指标刻意分开：

```text
每个 node 上的 NVIDIA driver 和 GPU
  → DCGM Exporter DaemonSet（:9400/metrics）
  → gpu-monitoring 中的 ServiceMonitor
  → monitoring 中的 Prometheus
  → Grafana 中的“Foretoken GPU 运行状态”Dashboard
```

model-server `/metrics` 继续提供当前推理后端的原生指标，例如 vLLM 请求时延、scheduler 状态和 KV cache 使用。GPU 利用率、framebuffer 显存、功耗、温度、时钟、限速和硬件错误则由 DCGM Exporter 提供。两类指标互相补充，不能彼此替代。

## 支持边界

仓库提供：

- kube-prometheus-stack values，只发现 `monitoring`、`foretoken-platform` 和 `gpu-monitoring` 中的 ServiceMonitor；
- 经过收紧的 NVIDIA DCGM Exporter chart `4.8.2` 和 image `4.5.3-4.8.2-distroless` values；
- 一条面向 standalone exporter 的叠加式 NetworkPolicy：当 cluster CNI 执行 NetworkPolicy 时，它允许 `monitoring` 中的 Pod 访问 TCP `9400` 端口；
- 不依赖固定 datasource 的中文 Grafana Dashboard；
- 一组不含 `DCGM_FI_PROF_*` profiling 字段的基础硬件指标。

这套配置不会：

- 把 GPU 指标塞进 model-server `/metrics`；
- 为 exporter 预留一个可调度的 `nvidia.com/gpu` 资源；
- 把某个物理 GPU 样本归属到 Foretoken ModelGroup 或 Pod；
- 安装告警、通知 receiver 或 Benchmark collector；
- 启用 DCGM profiling 指标，或取代 PyTorch Profiler 和 Nsight。

Exporter 以 UID `1000` 运行，丢弃所有 Linux capability，也不获得 `SYS_ADMIN`。仓库还清除了 upstream chart 默认的 `system-node-critical` 优先级，因为可选监控组件不应获得能够抢占推理 Pod 的优先级。它仍会消耗 chart 配置的 CPU 和内存：每个 exporter Pod 请求 `100m` CPU 和 `128Mi` 内存，limit 为 `200m` 和 `512Mi`。

Foretoken 通过显式 CLI 参数 `--kubernetes=false` 关闭 exporter 的 Kubernetes Pod 映射，因此基础硬件遥测不含 Pod 或 container 归属。Chart `4.8.2` 在映射关闭后仍会渲染 `/var/lib/kubelet/pod-resources` 的只读 hostPath mount；进程不会轮询它，但该 chart 版本无法通过 values 删除此 mount。请把它视为固定 upstream chart 的已知限制，而不是这套配置使用的能力。

## 前置条件

安装前请确认：

- 每个目标 GPU node 都能使用 NVIDIA driver 和 NVIDIA Container Toolkit；
- container runtime 会把 GPU 暴露给 container；
- `kubectl` 和 Helm 指向目标 cluster；
- 如果需要 ingress 隔离，cluster 使用能够执行 NetworkPolicy 的 CNI；
- 已确认 cluster 是否已经运行 GPU Operator 或其他 DCGM Exporter。

对于 k3d，请先完成[使用 k3d 部署 Foretoken](k3d-deployment_zh.md)。仓库的 k3d 配置将 NVIDIA 设为默认 runtime，因此 standalone exporter 不需要设置 `runtimeClassName`。

如果 GPU Operator 已经提供 exporter，不要重复安装。请复用已有 exporter，在 `deploy/observability/kube-prometheus-stack-values.yaml` 的副本中加入其 ServiceMonitor namespace，并让该 ServiceMonitor 添加以下 target label：

```yaml
relabelings:
  - targetLabel: foretoken_observability_source
    replacement: dcgm
```

仓库 Dashboard 使用这个稳定 label，不依赖随 Helm release 变化的 `job` 名。

复用已有 exporter 前，请检查一条原始 GPU series，确认它提供本 Dashboard 预期的大写 `UUID` label。DCGM Exporter 的 legacy namespace 模式可能使用小写 `uuid`；如果 label contract 不同，请先关闭 legacy 模式或适配 Dashboard。同时确认 Dashboard 使用的 metric family 已启用。

仓库提供的 `dcgm-exporter-network-policy.yaml` 匹配本指南中 standalone chart 的 label，不会自动选中 GPU Operator 管理的 exporter。复用其他 exporter 时，请检查真实 Pod label 和现有 policy，再按需提供由平台管理的 ingress policy。

## 1. 安装或配置 Prometheus 和 Grafana

平台已有 monitoring stack 时应直接复用。下面的命令适用于 Foretoken 开发 cluster，并固定为本仓库验证过的版本：

```bash
helm repo add prometheus-community \
  https://prometheus-community.github.io/helm-charts
helm repo update

helm upgrade --install kube-prometheus-stack \
  prometheus-community/kube-prometheus-stack \
  --version 88.5.4 \
  --namespace monitoring \
  --create-namespace \
  --values deploy/observability/kube-prometheus-stack-values.yaml \
  --wait \
  --debug
```

这份 values 在 namespace allowlist 内使用空的 ServiceMonitor object selector。这样既保留 kube-prometheus-stack 自己创建的 monitors，也发现 Foretoken 和 DCGM monitors；它不会发现所有 namespace 中的任意 ServiceMonitor。

Namespace allowlist 是 discovery 信任边界的一部分：能够在这些 namespace 中创建 ServiceMonitor 的主体，可以影响 Prometheus 抓取哪些 endpoint。请限制这些 namespace 的写权限；平台使用不同 namespace 名时，复制并修改 values 文件。

Prometheus 需要跨 workload namespace 访问动态 Foretoken model-server Service。只给实际运行可信 Prometheus Pod 的 namespace 添加 label：

```bash
kubectl label namespace monitoring \
  inference.foretoken.io/metrics-scraper=true \
  --overwrite
```

采集 DCGM 不需要这个 label。由于 Kubernetes NetworkPolicy 工作在 L3/L4，而不是 HTTP path 级别，这个 label 会允许该 namespace 中每个 Pod 访问共享 model-server 端口。不要给 `gpu-monitoring` 添加它。

## 2. 安装 DCGM Exporter

先安装 Prometheus Operator，确保 cluster 已有 `ServiceMonitor` CRD，再添加官方 exporter repository：

```bash
helm repo add gpu-helm-charts \
  https://nvidia.github.io/dcgm-exporter/helm-charts
helm repo update
```

在 exporter Pod 出现前先创建隔离 namespace 和 ingress policy：

```bash
kubectl create namespace gpu-monitoring \
  --dry-run=client \
  --output yaml \
  | kubectl apply --filename -

kubectl apply \
  --filename deploy/observability/dcgm-exporter-network-policy.yaml
```

请在下面两种安装命令中选择一种。

### 标准 Kubernetes GPU node

```bash
helm upgrade --install dcgm-exporter \
  gpu-helm-charts/dcgm-exporter \
  --version 4.8.2 \
  --namespace gpu-monitoring \
  --create-namespace \
  --values deploy/observability/dcgm-exporter-values.yaml \
  --wait \
  --debug
```

### 选择了宿主机 GPU 的 k3d

k3d 会在 privileged node container 内再次运行 NVIDIA container runtime。固定的 image 默认使用 `NVIDIA_VISIBLE_DEVICES=all`；如果不覆盖，内层 runtime 可能尝试挂载它能枚举的每张宿主机 GPU，包括本开发 cluster 本不打算使用的设备。请使用配置 device plugin 时相同的可信 `GPU_INDICES` 选择：

```bash
export GPU_INDICES=6,7

helm upgrade --install dcgm-exporter \
  gpu-helm-charts/dcgm-exporter \
  --version 4.8.2 \
  --namespace gpu-monitoring \
  --create-namespace \
  --values deploy/observability/dcgm-exporter-values.yaml \
  --set-json \
    "extraEnv=[{\"name\":\"NVIDIA_VISIBLE_DEVICES\",\"value\":\"${GPU_INDICES}\"}]" \
  --wait \
  --debug
```

这是环境专用 override，不是仓库默认值。宿主机 index 在 k3d 内可能变成 `gpu="0"` 和 `gpu="1"`；请使用逐卡 legend 中保留的 `UUID` label 与 `nvidia-smi` 对照。

Override 会让 exporter 监控目标 GPU 集合，但不会申请或占用 Kubernetes `nvidia.com/gpu` 配额。在已验证的双 GPU k3d 环境中，Prometheus 恰好收到两个 GPU UUID，Kubernetes 也登记了两张可调度 GPU。

这不会把 privileged 嵌套 k3d 变成租户隔离边界：在某些 runtime 上，node 仍能枚举其他宿主机 GPU，可信进程也可以覆盖基于环境变量的选择。只将这条路径用于受控开发；不要在共享宿主机上把 exporter 设为 `all`；不可信 workload 应使用常规 node 或 VM 隔离。

### 同时包含 CPU 和 GPU node 的 cluster

Upstream chart 默认创建 DaemonSet，但不带 node selector。在混合 cluster 中使用前，请复制 values 文件，并使用平台实际维护的 label 只选择 GPU node，例如：

```yaml
nodeSelector:
  nvidia.com/gpu.present: "true"
```

GPU Feature Discovery 通常会维护该 label，但请先用 `kubectl get nodes --show-labels` 验证。不要为不存在或不准确的 label 添加 selector。

### 非默认 NVIDIA runtime

不要因为 cluster 有 NVIDIA GPU 就直接设置 runtime class。只有 cluster 需要显式 RuntimeClass，且 `kubectl get runtimeclass nvidia` 成功时，才在环境专用 values overlay 中添加：

```yaml
runtimeClassName: nvidia
```

仓库通用 values 刻意省略它，因为没有该 RuntimeClass 的 cluster 会导致 DaemonSet 无法调度。

## 3. 加载 Grafana Dashboard

Dashboard 以原始 JSON 为唯一来源，由 Kustomize 包装成 ConfigMap：

```bash
kubectl --namespace monitoring apply \
  --kustomize deploy/observability/grafana
```

kube-prometheus-stack 的 Grafana sidecar 会发现 `grafana_dashboard=1` label，并加载名为 `Foretoken GPU 运行状态` 的 Dashboard。Dashboard 会动态选择 Prometheus datasource，并可按抓取 instance 和 exporter 内可见 GPU index 过滤。逐卡 legend 保留设备 UUID，便于将 index 与物理设备对照。

如果平台 Grafana 不使用标准 dashboard sidecar，可以在 Grafana 中手动导入 `deploy/observability/grafana/dashboards/foretoken-gpu-dashboard.json`。

## 4. 启用 Foretoken 指标

GPU 监控不依赖 Foretoken 请求路由。若还要采集 Frontend 和 model-server 指标，请在安装或升级 Foretoken 时加入：

```yaml
observability:
  serviceMonitor:
    enabled: true
```

两个 Foretoken ServiceMonitor 位于 `foretoken-platform`，但会在 cluster 范围发现匹配的 workload Service。DCGM ServiceMonitor 位于 `gpu-monitoring`，只选择同一 namespace 中的 exporter Service。

## 逐层验证

请从 producer 向 Dashboard 逐层验证。这个顺序可以定位真正断开的边界，而不是把“空 panel”本身当成根因。

### 1. Exporter Pod 和 Service

```bash
kubectl --namespace gpu-monitoring rollout status \
  daemonset/dcgm-exporter \
  --timeout=3m

kubectl --namespace gpu-monitoring get \
  daemonset,pods,service,servicemonitor
```

DaemonSet 的 desired 和 ready 数量应等于选中的 GPU node 数。Exporter 应包含 CPU/内存 request，但不应含有 `nvidia.com/gpu` request 或 limit：

```bash
kubectl --namespace gpu-monitoring get daemonset dcgm-exporter \
  --output yaml
```

### 2. 原始 OpenMetrics 输出

Port-forward 只绑定 loopback：

```bash
kubectl --namespace gpu-monitoring port-forward \
  --address 127.0.0.1 \
  service/dcgm-exporter \
  9400:9400
```

在另一个 terminal 中运行：

```bash
curl --fail --silent http://127.0.0.1:9400/metrics \
  | grep 'DCGM_FI_DEV_GPU_TEMP'
```

每张 exporter 可见 GPU 出现一条 series，说明 DCGM 能够读取设备。验证后停止 port-forward。

### 3. Prometheus discovery

在 Prometheus 中确认 DCGM target 为 `UP`，然后查询：

```promql
min(up{foretoken_observability_source="dcgm"})
```

结果应为 `1`。使用下面的查询统计唯一的 exporter 可见 GPU：

```promql
count(
  group by (instance, gpu, UUID) (
    DCGM_FI_DEV_GPU_TEMP{foretoken_observability_source="dcgm"}
    or DCGM_FI_DEV_GPU_UTIL{foretoken_observability_source="dcgm"}
    or DCGM_FI_DEV_FB_USED{foretoken_observability_source="dcgm"}
  )
)
```

双 GPU k3d node 的预期结果是 `2`。这个数量与 Kubernetes allocatable GPU 含义不同：DCGM 报告 exporter 可见的设备，device plugin 则登记可供调度的资源。

### 4. Grafana

```bash
kubectl --namespace monitoring get configmap \
  foretoken-gpu-dashboard

kubectl --namespace monitoring port-forward \
  --address 127.0.0.1 \
  service/kube-prometheus-stack-grafana \
  3000:80
```

打开 `http://127.0.0.1:3000`，搜索 `Foretoken GPU 运行状态`。无论 Grafana 账号选择哪种界面语言，Dashboard 自身的文字都保持中文。查看后停止 port-forward。

## 指标与 Dashboard 语义

自定义 collector 是经过选择的基础硬件集合，不是 DCGM 所有字段的清单。它包括：

| 范围 | 代表字段 |
| --- | --- |
| 利用率 | GPU、memory-copy、encoder、decoder 利用率 |
| 显存 | Framebuffer 已用和空闲 |
| 温度与功耗 | GPU/显存温度、当前功耗、累计能耗 |
| 时钟 | SM 和显存时钟 |
| 限速 | 功耗、温度、板卡、可靠性及相关 violation counter |
| PCIe 与 NVLink | Replay、带宽、CRC、replay、recovery counter |
| 可靠性 | ECC 总数、retired page、remapped row 和 XID |

配置了字段不代表运行时一定会输出。特定 GPU、driver、DCGM 或 exporter 组合可能不提供某个字段。在验证过的 A100 环境中，配置了 37 项字段，其中 31 项产生了 series；PCIe TX/RX throughput、XID 和三个 retired-page 字段没有上报。Exporter 日志没有提供字段级根因，因此缺失必须解释为**不可用或未报告**，绝不能转换为 0。

Dashboard 同样遵守这一规则：

- 当前值 card 使用 instant query，不会把可见时间范围内的旧样本冒充“当前状态”；
- 数据空缺保持为空，不填成 0；
- 可选 series 缺失时保留 Grafana `No data`，不转换为 0；`未报告` 只是已返回 field 的值为 null 时的 fallback；
- `min(up{...})` 会在任意一个选中的 exporter target 掉线时报告异常；
- PCIe、NVLink 和限速增量使用固定的 5 分钟窗口；
- 逐卡 series 在降维或聚合时保留 `(instance, gpu, UUID)`。

Dashboard 颜色是运维提示，不是告警规则。GPU 数量使用中性颜色，因为预期数量取决于部署。温度和 PCIe replay 阈值是通用可视化默认值；将它们用于告警前，必须按已部署 GPU 型号、平台策略和 workload 校准。

`gpu` label 是 exporter node 内可见的 index。Container 和 k3d 的 GPU 过滤可能重新编号，所以 `gpu="0"` 不一定是宿主机物理 GPU 0。与 `nvidia-smi` 对照时请使用 `UUID` label。

这套 baseline 不启用 Pod attribution、MIG 专用 panel 或 vGPU panel。它们没有出现，表示当前 Dashboard 不支持，不能据此证明平台没有 MIG 或 vGPU 配置。

## 排障

### Exporter Pod 为 Pending

- 检查 node selector、taint 和 toleration；
- 检查显式配置的 RuntimeClass 是否存在；
- 在 k3d 中检查 node container 是否带有目标 GPU 和 NVIDIA runtime mount。

### Exporter Pod 正在运行，但没有硬件指标

- 查看 `kubectl --namespace gpu-monitoring logs daemonset/dcgm-exporter`；
- 在宿主机和 NVIDIA runtime container 内分别用 `nvidia-smi` 验证 driver；
- 确认 exporter 能看到目标 device file；
- 使用仓库 values 时，log 应说明 NVML provider 已跳过，也不应反复出现 pod-resources socket 警告。若仍出现，请检查实际渲染的 container arguments 和 Helm values merge：运行中的 release 未使用这套 baseline。复用或旧版 exporter 若启用了 Kubernetes mapping，可能产生这类警告；它们会影响 Pod attribution，但本身不能证明基础硬件遥测失败。

### ServiceMonitor 不存在

- 先安装 Prometheus Operator 及其 CRD，再安装 DCGM Exporter；
- 确认 `serviceMonitor.enabled=true` 已进入 Helm release；
- 排查 values merge 时，用仓库 values 静态渲染固定版本 chart。

### Prometheus target 缺失或为 down

- 确认 ServiceMonitor object 位于允许的 namespace；
- 检查它的 Service selector、名为 `metrics` 的端口、endpoint 和 namespace selector；
- 检查 `dcgm-exporter-metrics` NetworkPolicy 和 Prometheus 实际所在 namespace；Kubernetes NetworkPolicy 会叠加，其他选中同一 Pod 的 policy 可能放行额外来源；
- 如果 monitoring stack 使用其他 namespace，请同时复制并一致修改 Prometheus values 与 NetworkPolicy。

### Dashboard 已加载但没有数据

- 确认 Prometheus 能查询到 `up{foretoken_observability_source="dcgm"}`；
- 选择正确的 Prometheus datasource，将 instance/GPU filter 恢复为 `All`；
- 确认 Dashboard ConfigMap 位于 Grafana sidecar 监视的 namespace；
- 不受支持的字段会保持为空，请先检查 GPU 温度或利用率。

### GPU series 重复

不要在同一批 node 上同时运行 standalone DCGM Exporter 和 GPU Operator 管理的 exporter。如果同一个 exporter 被多个 ServiceMonitor 重复抓取，应删除重复 discovery 路径，而不是只在 Grafana 中隐藏。

## 删除或回滚

这些组件具有独立所有权。只删除为开发 cluster 安装的资源：

```bash
helm uninstall dcgm-exporter \
  --namespace gpu-monitoring

kubectl delete \
  --filename deploy/observability/dcgm-exporter-network-policy.yaml

kubectl --namespace monitoring delete \
  --kustomize deploy/observability/grafana
```

不要在清理 Foretoken 时卸载共享 GPU Operator、Prometheus stack 或 Prometheus Operator CRD。只有 kube-prometheus-stack 专属于可删除的开发 cluster、且没有其他 workload 依赖时，才能删除该 release。

## Upstream 参考资料

- [安装 DCGM Exporter](https://docs.nvidia.com/datacenter/dcgm/latest/installation/install-dcgm-exporter.html)
- [DCGM Exporter 字段、单位和 label](https://docs.nvidia.com/datacenter/dcgm/latest/reference/dcgm-exporter-metrics.html)
- [NVIDIA DCGM Exporter `4.5.3-4.8.2` Helm chart values](https://github.com/NVIDIA/dcgm-exporter/blob/4.5.3-4.8.2/deployment/values.yaml)
- [Grafana Dashboard variable](https://grafana.com/docs/grafana/latest/dashboards/variables/)
