<!--
SPDX-License-Identifier: Apache-2.0
SPDX-FileCopyrightText: Copyright contributors to the Foretoken project
-->

# Foretoken 命令行工具

[English](README.md) | 简体中文

Foretoken 命令行工具通过统一的 `foretoken` 入口安装 Kubernetes 平台、从 Kustomize 配置部署模型服务、查看服务就绪状态、解析前端访问入口并运行评测。

## 开始前

需要准备 Python 3.11 或更高版本、`kubectl`，以及 Helm 4.2.4 或更高版本。可以按下文创建本地集群，或选择已有集群的 Kubernetes context。GPU 节点需要安装厂商驱动和 Kubernetes device plugin。

## 安装命令行工具

使用 pip 安装发布的命令行工具：

```bash
pip install foretoken

# 从源码目录安装：
# pip install -e .
```

或使用 uv 创建并激活虚拟环境后安装：

```bash
uv venv
source .venv/bin/activate
uv pip install foretoken
```

运行 `foretoken --version` 查看已安装的 CLI 版本。

## 创建本地集群

在已安装 Docker、NVIDIA Container Toolkit 和 k3d 的 Linux GPU 主机上创建本地 GPU 集群：

```bash
# 为本地集群命名，并使用 nvidia-smi 显示的 GPU 编号 0。
# 若使用两张 GPU，传入 --gpus 0,1。
foretoken cluster create k3d --name foretoken-dev --gpus 0
```

创建本地 kind 开发集群时，安装 Docker、kind、kubectl 和 Helm：

```bash
foretoken cluster create kind --name foretoken-dev
```

删除由 CLI 创建的集群：

```bash
foretoken cluster delete k3d --name foretoken-dev
foretoken cluster delete kind --name foretoken-dev
```

## 安装 Kubernetes 平台

`foretoken install` 在当前 Kubernetes context 中安装或更新共享的 Foretoken CRD 和依赖。平台资源使用 `foretoken-platform` 命名空间。该命令还会配置监控，并在网关模式下配置 Gateway 资源。`foretoken deploy` 在各个部署命名空间中创建控制器和模型服务。

安装会设置后续部署使用的默认配置，不会重启已有控制器或服务。重新部署服务后，才会采用更新后的默认配置。

### 默认安装

默认安装已发布的平台，并为模型部署选择通过 `LoadBalancer` Service 直接访问的方式：

```bash
foretoken install
```

安装会自动选择 NVIDIA 或沐曦运行时，并复用或安装 LeaderWorkerSet 控制器和 RDMA 设备插件。`--values` 中显式指定的运行时配置优先。混合 GPU 集群通过 `runtime.vllm.gpu.resourceName` 选择资源，或通过 `runtime.vllm.gpu.nodeSelector` 限定节点范围。

安装默认启用日志采集和持久化。配置、日志查询、看板和告警见[可观测性](../observability/README_zh.md)。

### 网关模式

网关模式会创建专用的 `GatewayClass` 和 `Gateway`，集群没有可复用的控制器时自动安装 Envoy Gateway：

```bash
foretoken install --frontend-mode gateway
```

使用其他 Gateway Controller 时，应复用由该 Controller 管理的 Gateway：

```bash
foretoken install \
  --frontend-mode gateway \
  --gateway-name inference-gateway \
  --gateway-namespace gateway-system
```

只有多个 listener 都符合条件时，才需要添加 `--gateway-section-name LISTENER`。

### 当前源码

集群需为应用文件提供默认存储类（StorageClass）。在 Foretoken 仓库根目录运行：

```bash
foretoken install -e .
```

`foretoken deploy` 会从当前 Foretoken 目录构建控制器、前端和模型服务的改动。并行运行不同代码版本，见[多版本部署示例](../docs/deployment/source_zh.md#并行运行候选方案)；使用本地 vLLM 或沐曦插件，见[引擎源码部署](../docs/deployment/source_zh.md#修改推理引擎)。

当前 context 是标准 kind 或 k3d 时，命令直接在节点载入构建好的镜像；其他 Kubernetes context 需要构建 Pod 和节点都能访问的镜像仓库。使用无认证的内网仓库时：

```bash
foretoken install -e . --registry registry.example.com:5000/foretoken
```

如果仓库需要认证，安装前按[源码部署指南](../docs/deployment/kubernetes_zh.md)配置拉取 Secret。

### 模型分发

使用 Dragonfly 在节点间共享公开模型文件时，在 `deploy/platform-values.yaml` 中配置：

```yaml
modelDistribution:
  dragonfly:
    enabled: true
```

使用已发布平台时，执行：

```bash
foretoken install --values deploy/platform-values.yaml
```

源码安装从仓库根目录执行 `foretoken install -e . --values deploy/platform-values.yaml`，保留原镜像仓库和引擎源码选项。

安装命令会准备 Dragonfly，或复用已有安装。需要身份认证的模型及自定义模型源仍直接从源站下载。要选择特定的 Dragonfly Helm release，在 `modelDistribution.dragonfly` 下设置 `existingRelease: {name: dragonfly, namespace: dragonfly-system}`。

在具备 RDMA 的 NVIDIA 集群上，还可以通过 ModelExpress 从运行中的副本加载权重。在同一个 `modelDistribution` 下加入：

```yaml
modelDistribution:
  modelexpress:
    enabled: true
```

自动权重传输适用于使用持久缓存、数据并行度为 1、未启用专家动态负载均衡的远端模型；显式设置的 `load-format` 保持不变。每个 GPU worker 选择拓扑邻近的可用 RDMA 网卡，没有兼容源副本时加载已准备的文件。

修改设置后重新执行上述安装命令，再部署需要更新的服务。将 `enabled` 设为 `false` 即可关闭对应能力。`foretoken uninstall` 在没有工作负载使用时移除托管的 Dragonfly，复用的安装会保留。

### 安装选项

需要自定义平台设置时，使用 `--values`。没有显式覆盖时，每次安装会为默认平台镜像和 Chart 下载选择公共来源。自动选中的代理失败时，Chart 下载和源码构建中的镜像拉取可回退到源站。通过 `--oci-registry` 指定仓库，values 中明确填写的镜像地址保持不变。选源在运行 CLI 的机器上执行，所选仓库也需要能从集群节点访问。

模型服务通过一个集群外可访问的 IP 提供服务。k3d、k3s 和云上集群会自动分配这个 IP；用 kubeadm、RKE2 或 kubespray 搭建的集群默认没有地址分配能力，安装结尾会提示 `LoadBalancer support Not verified`。此时向集群管理员确认一段节点网段内未被占用的 IP 交给 Foretoken，由它分配给服务：

```yaml
loadBalancer:
  managedAddresses:
    - 192.168.1.240-192.168.1.250
```

## 部署和管理模型服务

从[快速开始](../README_zh.md)准备的仓库目录执行，部署一个 Kustomize 根目录中的前端服务和全部模型。

资源要求见[多模型示例](../examples/quickstart/multi-model/README_zh.md)，目录和 PVC 配置见[模型存储](../docs/features/model-storage_zh.md)。单模型部署使用 `examples/quickstart/qwen3-0.6b`。

```bash
foretoken deploy examples/quickstart/multi-model --timeout 20m
```

命令会安装或更新部署所在命名空间的控制器，并应用所选服务的配置。同一命名空间中的服务共用该控制器；需要独立运行不同代码版本时，使用不同命名空间。

等待期间显示服务状态，并输出带 Pod/容器来源标识的日志。运行中的服务达到 Ready、零副本服务停止、所选告警配置完成后，命令退出。未指定 `--timeout` 时最多等待十分钟。告警配置见[服务可观测性示例](../examples/feature/observability/README_zh.md)。

目标模型配置被拒绝时，部署命令会显示原因并失败退出，即使旧版本仍在提供服务。模型的 `spec.timeouts.startup` 还限定运行工作负载创建后等待启动或恢复就绪的时间，模型准备阶段单独计时。超过时限会结束本次命令，Kubernetes 继续恢复；时限内的容器重启仍按进行中处理。

不应用配置，直接查看同一部署的状态：

```bash
foretoken status examples/quickstart/multi-model
```

状态输出包含 Kubernetes 的等待与退出消息，重启后也保留上一次容器退出的诊断。查看命名空间中的全部服务，或添加 `--watch` 持续查看状态变化与完整 Pod/容器日志，按 Ctrl+C 结束：

```bash
foretoken status -n foretoken-multi-model-demo
foretoken status -n foretoken-multi-model-demo --watch
```

部署后解析前端服务的公开 URL：

```bash
FORETOKEN_FRONTEND_URL="$(foretoken endpoint examples/quickstart/multi-model)"
```

HTTP Gateway 模式下，单独解析请求的 `Host`：

```bash
FORETOKEN_REQUEST_HOST="$(foretoken endpoint examples/quickstart/multi-model --host)"
```

直接访问时，`--host` 返回主机名或 IP，以及 URL 中包含的端口；HTTP Gateway 模式下返回配置的路由域名。`foretoken endpoint` 等待 LoadBalancer 或 Gateway 分配地址；要等待服务就绪，请使用 `foretoken deploy`。

### 停止与恢复

```bash
foretoken stop examples/quickstart/multi-model
```

实验暂时不用 GPU 时，可以停止服务，同时保留配置、存储和构建缓存。命令等待前端和模型实例退出；之后对同一目录执行 `foretoken deploy` 即可恢复，模型进程会重新启动并加载权重。

## 性能评测：响应速度与吞吐量

使用 `foretoken perf` 测量响应延迟、请求吞吐量和 token 生成速度。传入 Kustomize 目录，或用 `--url` 和 `--model` 指定已有服务。各类负载见[性能评测示例](../benchmarks/docs/perf/README_zh.md)。

## 质量评测：回答评分与模型对比

使用 `foretoken eval`，通过 lm-evaluation-harness 或 EvalScope 为模型回答评分。服务选择方式与性能评测相同，任务和判分参数采用所选框架的写法。具体命令见[质量评测](../benchmarks/docs/eval/README_zh.md)。添加 `--reference` 可[比较候选与参考模型的概率分布](../benchmarks/docs/eval/distribution-comparison_zh.md)。

## 导出图表

评测时用 `--output plot` 导出图表，已有结果用 `foretoken plot RESULT_DIR [RESULT_DIR ...]` 重绘或对照。指标、图宽和输出位置见[绘图示例](../benchmarks/docs/recipes_zh.md#绘制与对照结果)。

## 性能剖析：执行瓶颈

在 `foretoken deploy` 或 `foretoken perf` 后加 `--profile`，记录 CPU/GPU 执行过程，再用 `foretoken profile view` 浏览结果。环境准备与采集命令见[性能剖析](../benchmarks/docs/profile/README_zh.md)。

## 清理

先删除服务及其不再使用的构建资源，再卸载平台：

```bash
foretoken delete examples/quickstart/multi-model
foretoken uninstall
```

删除命令会先等待服务完成清理，再移除命名空间控制器。该命名空间中仍有其他 Foretoken 资源时，控制器继续保留。Kustomize 配置中包含的命名空间及其全部资源会在最后删除。

卸载保留 CRD、日志存储和复用的集群组件。仍有工作负载依赖托管的 LeaderWorkerSet 或 MetalLB 时，也会保留对应控制器。
