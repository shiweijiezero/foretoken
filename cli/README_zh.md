<!--
SPDX-License-Identifier: Apache-2.0
SPDX-FileCopyrightText: Copyright contributors to the Foretoken project
-->

# Foretoken 命令行工具

[English](README.md) | 简体中文

Foretoken 命令行工具通过统一的 `foretoken` 入口安装 Kubernetes 平台、从 Kustomize 配置部署模型服务、查看服务就绪状态、解析前端访问入口并运行评测。

## 开始前

需要准备 Python 3.10 或更高版本、当前 Kubernetes context、`kubectl` 和 Helm。GPU 节点需要预先安装厂商驱动和 Kubernetes device plugin。

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

安装同时会配置监控；集群里已有 Prometheus 和 GPU 指标 exporter 时直接复用。详见[可观测性](../observability/README_zh.md)。

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

重复使用 `--values` 可提供平台镜像、runtime 和硬件配置。

模型服务通过一个集群外可访问的 IP 提供服务。k3d、k3s 和云上集群会自动分配这个 IP；用 kubeadm、RKE2 或 kubespray 搭建的集群默认没有地址分配能力，安装结尾会提示 `LoadBalancer support Not verified`。此时向集群管理员确认一段节点网段内未被占用的 IP 交给 Foretoken，由它分配给服务：

```yaml
loadBalancer:
  managedAddresses:
    - 192.168.1.240-192.168.1.250
```

```bash
foretoken install --values platform-values.yaml
```

### 持久化运行时缓存

在 workload namespace 中创建一个 `RuntimeCache`，Foretoken 即可自动创建并管理共享缓存 PVC。已有 PVC 仍可通过 `workload.cache.claimName` 使用。详见[持久化运行时缓存](../docs/development/runtime-cache_zh.md)。

## 部署和管理模型服务

部署一个 Kustomize 根目录中的前端服务和全部模型。以下命令在仓库根目录执行；尚未获取配置时，先运行：

```bash
git clone https://github.com/shiweijiezero/foretoken.git
cd foretoken
```

资源和存储要求见[多模型示例](../examples/multi-model-quickstart/README_zh.md)。单模型部署使用 `examples/quickstart`。

```bash
foretoken deploy examples/multi-model-quickstart --timeout 20m
```

该命令会应用配置、输出服务状态变化，并在所有服务就绪后退出。未指定 `--timeout` 时最多等待十分钟。

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

## 评测模型服务

使用 `foretoken bench` 评测模型服务性能，命令和示例见[模型服务性能评测](../benchmarks/README_zh.md)。

## 清理

先删除部署的服务，再卸载平台：

```bash
foretoken delete examples/multi-model-quickstart
foretoken uninstall
```

卸载保留 Foretoken CRD 和复用的集群组件。如果其他服务仍依赖托管的 MetalLB，也会保留它。
