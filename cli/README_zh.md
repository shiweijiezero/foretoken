<!--
SPDX-License-Identifier: Apache-2.0
SPDX-FileCopyrightText: Copyright contributors to the Foretoken project
-->

# Foretoken 命令行工具

[English](README.md) | 简体中文

Foretoken 命令行工具通过统一的 `foretoken` 入口安装 Kubernetes 平台、从 Kustomize 配置部署模型服务、查看服务就绪状态、解析前端访问入口并运行评测。

新集群从“安装命令行工具”开始。如果 `foretoken --version` 已经可用，直接安装平台；如果集群已经安装 Foretoken 平台，直接部署模型服务。

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

安装过程中，命令行工具会发现 Prometheus 和加速器指标 exporter，复用兼容的共享实例，按需安装 Prometheus 和 NVIDIA DCGM Exporter，并接入沐曦集群已经提供的 mxExporter。监控选择与配置见[可观测性](../observability/README_zh.md)。

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

重复使用 `--values` 可提供平台镜像、runtime 和硬件配置。发布镜像安装与源码安装模式会记录在 Helm 元数据中，不能静默切换。原本通过 Helm 直接安装的发布实例继续使用原有 Helm 生命周期，命令行工具不会自动接管。

如果安装结尾出现 `LoadBalancer support Not verified`，说明集群还无法给模型服务分配外部 IP。向集群管理员要几个节点所在网络中空闲的 IP，写进 values 文件，再运行 `foretoken install --values PATH`：

```yaml
loadBalancer:
  managedAddresses:
    - 192.168.1.240-192.168.1.250
```

Foretoken 会安装把这些 IP 分配给服务的组件，并记住这份列表，之后安装不必再传。

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

## 运行评测

使用 pip 安装可选的评测依赖：

```bash
pip install 'foretoken[bench]'

# 如果使用源码安装：
# pip install -e .
# pip install -e '.[bench]'
```

或在已经激活的 uv 虚拟环境中安装评测依赖：

```bash
uv pip install 'foretoken[bench]'
```

然后运行评测：

```bash
foretoken bench examples/multi-model-quickstart --model Qwen/Qwen3-0.6B
```

命令行工具使用当前 `kubectl` context，并遵循 `KUBECONFIG` 等标准 Kubernetes 配置。

## 清理

删除同一配置渲染出的资源：

```bash
foretoken delete examples/multi-model-quickstart
```

该命令会等待删除完成，并忽略已经不存在的资源。删除全部 Foretoken 服务后，可以移除平台发布实例：

```bash
foretoken uninstall
```

该命令保留 Foretoken CRD，并在仍有用户服务时拒绝卸载。平台卸载时会一并删除由命令行工具管理的监控、Gateway 和 MetalLB 资源，复用的集群组件保持不变。
