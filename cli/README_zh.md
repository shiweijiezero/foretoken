<!--
SPDX-License-Identifier: Apache-2.0
SPDX-FileCopyrightText: Copyright contributors to the Foretoken project
-->

# Foretoken 命令行工具

[English](README.md) | 简体中文

Foretoken 命令行工具通过统一的 `foretoken` 入口安装 Kubernetes 平台、从 Kustomize 配置部署模型服务、查看服务就绪状态、解析前端访问入口并运行评测。

## 开始前

需要准备 Python 3.11 或更高版本、当前 Kubernetes context、`kubectl` 和 Helm。GPU 节点需要预先安装厂商驱动和 Kubernetes device plugin。

## 安装命令行工具

使用 pip 安装已经发布的 Foretoken 命令行工具包：

```bash
pip install foretoken

# 如果使用源码安装：
# pip install -e .
```

或使用 uv 创建并激活虚拟环境后安装：

```bash
uv venv
source .venv/bin/activate
uv pip install foretoken
```

运行 `foretoken --version` 查看已安装的命令行工具版本。

## 安装 Kubernetes 平台

`foretoken install` 会在当前 Kubernetes context 中安装 Foretoken CRD 和控制器。平台资源固定使用 `foretoken-platform` 命名空间。该命令还会配置监控，并在网关模式下配置 Gateway 资源。模型服务通过 `foretoken deploy` 单独部署。

### 默认安装

默认使用发布镜像，并通过 `LoadBalancer` Service 提供本地访问入口：

```bash
foretoken install
```

安装会自动选择 NVIDIA 或沐曦运行时，并复用或安装 LeaderWorkerSet 控制器和 RDMA 设备插件。`--values` 中显式指定的运行时配置优先。混合 GPU 集群通过 `runtime.vllm.gpu.resourceName` 选择资源，或通过 `runtime.vllm.gpu.nodeSelector` 限定节点范围。

看板和告警的使用见[可观测性](../observability/README_zh.md)。

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

按[源码部署指南](../docs/custom-deployment_zh.md)准备构建工具，再从仓库根目录安装：

```bash
foretoken install -e .
```

当前 context 是标准 kind 或 k3d 时，命令会构建并导入本地镜像；其他 Kubernetes context 需要提供节点可访问的 registry。安装前先使用有目标仓库推送权限的账户登录 registry：

```bash
docker login ghcr.io
foretoken install -e . --registry ghcr.io/example/foretoken
```

登录 registry 用于授权本机推送镜像。私有 registry 还需要通过 `--values` 配置 `imagePullSecrets` 和 `workload.imagePullSecrets`，让节点能够拉取镜像，详见[从源码部署 Foretoken](../docs/custom-deployment_zh.md)。

### 安装选项

自定义平台镜像、runtime 或硬件设置时使用 `--values`。没有显式覆盖时，安装会为默认平台镜像和 OCI Chart 比较可用的公共来源。通过 `--oci-registry` 指定仓库，values 中明确填写的镜像地址保持不变。选源在运行 CLI 的机器上执行，所选仓库也需要能从集群节点访问。

模型服务通过一个集群外可访问的 IP 提供服务。k3d、k3s 和云上集群会自动分配这个 IP；用 kubeadm、RKE2 或 kubespray 搭建的集群默认没有地址分配能力，安装结尾会提示 `LoadBalancer support Not verified`。此时向集群管理员确认一段节点网段内未被占用的 IP 交给 Foretoken，由它分配给服务：

```yaml
loadBalancer:
  managedAddresses:
    - 192.168.1.240-192.168.1.250
```

## 部署和管理模型服务

从[快速开始](../README_zh.md)准备的仓库目录执行，部署一个 Kustomize 根目录中的前端服务和全部模型。

资源要求见[多模型示例](../examples/multi-model-quickstart/README_zh.md)，目录和 PVC 配置见[模型存储](../docs/model-storage_zh.md)。单模型部署使用 `examples/quickstart`。

```bash
foretoken deploy examples/multi-model-quickstart --timeout 20m
```

该命令会应用配置，同时显示服务就绪状态和各容器的启动进度，包括所在节点、镜像拉取失败、重启次数，以及引擎原生的权重加载和图捕获进度。不同 Pod 和重启尝试分别记录。有计数的阶段显示原生进度，没有计数的阶段显示状态，不估算百分比。所有服务就绪且所选告警配置完成后，命令才会退出。未指定 `--timeout` 时最多等待十分钟。告警配置放在服务的 Kustomize 部署中，见[服务可观测性示例](../examples/observability/README_zh.md)。

不应用配置，直接查看同一部署的状态：

```bash
foretoken status examples/multi-model-quickstart
```

查看一个命名空间中的全部 Foretoken 服务，或持续观察状态变化：

```bash
foretoken status -n foretoken-multi-model-demo
foretoken status -n foretoken-multi-model-demo --watch
```

部署后解析前端服务的公开 URL：

```bash
FORETOKEN_FRONTEND_URL="$(foretoken endpoint examples/multi-model-quickstart)"
```

HTTP Gateway 模式下，单独解析请求的 `Host`：

```bash
FORETOKEN_REQUEST_HOST="$(foretoken endpoint examples/multi-model-quickstart --host)"
```

直接访问时，`--host` 返回主机名或 IP，以及 URL 中包含的端口；HTTP Gateway 模式下返回配置的路由域名。`foretoken endpoint` 等待 LoadBalancer 或 Gateway 分配地址；要等待服务就绪，请使用 `foretoken deploy`。

## 性能评测：响应速度与吞吐量

使用 `foretoken perf` 测量响应延迟、请求吞吐量和 token 生成速度。传入 Kustomize 目录，或用 `--url` 和 `--model` 指定已有服务。各类负载见[性能评测示例](../benchmarks/docs/perf/README_zh.md)。

## 质量评测：回答正确率

使用 `foretoken eval`，通过 lm-evaluation-harness 或 EvalScope 为模型回答评分。服务选择方式与性能评测相同，任务和判分参数采用所选框架的写法。具体命令见[质量评测](../benchmarks/docs/eval/README_zh.md)。

## 性能剖析：执行瓶颈

在 `foretoken deploy` 或 `foretoken perf` 后加 `--profile`，记录 CPU/GPU 执行过程，再用 `foretoken profile view` 浏览结果。环境准备与采集命令见[性能剖析](../benchmarks/docs/profile/README_zh.md)。

## 清理

先删除部署的服务，再卸载平台：

```bash
foretoken delete examples/multi-model-quickstart
foretoken uninstall
```

卸载保留 CRD 和复用的集群组件。仍有工作负载依赖托管的 LeaderWorkerSet 或 MetalLB 时，也会保留对应控制器。
