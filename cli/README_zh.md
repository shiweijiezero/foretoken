<!--
SPDX-License-Identifier: Apache-2.0
SPDX-FileCopyrightText: Copyright contributors to the Foretoken project
-->

# Foretoken CLI

[English](README.md) | 简体中文

Foretoken CLI 通过统一的 `foretoken` 入口安装 Kubernetes 平台、从 Kustomize 配置部署模型服务、查看服务就绪状态、解析前端访问入口并运行评测。

新集群从“安装 CLI”开始。如果 `foretoken --version` 已经可用，直接安装平台；如果集群已经安装 Foretoken 平台，直接部署模型服务。

## 开始前

需要准备 Python 3.10 或更高版本、当前 Kubernetes context、`kubectl` 和 Helm。GPU 节点需要预先安装厂商驱动和 Kubernetes device plugin。源码安装还需要 Docker 和 Make，以及本地 kind/k3d 集群或所有目标节点都能访问的 OCI registry。

## 安装 CLI

使用 pip 安装 Foretoken CLI：

```bash
pip install -e .
```

或使用 uv 创建并激活虚拟环境后安装：

```bash
uv venv
source .venv/bin/activate
uv pip install -e .
```

这一步只会在当前 Python 环境中安装 `foretoken` 命令，不会修改 Kubernetes 集群。运行 `foretoken --version` 可以查看 CLI 及其对应的平台版本。

## 安装 Kubernetes 平台

`foretoken install` 会在当前 Kubernetes context 中安装 Foretoken CRD 和控制器。平台资源固定使用 `foretoken-platform` 命名空间。该命令还会配置监控，并在网关模式下配置 Gateway 资源。模型服务通过 `foretoken deploy` 单独部署。

### 默认安装

默认使用发布镜像，并通过 `LoadBalancer` Service 提供本地访问入口：

```bash
foretoken install
```

安装过程中，CLI 会发现 Prometheus 和加速器指标 exporter。它会复用兼容的共享实例，按需安装由 CLI 管理的 Prometheus 和 NVIDIA DCGM Exporter，并接入沐曦集群已经提供的 mxExporter。CLI 不安装 GPU 驱动、device plugin 或厂商 Operator。监控实例存在歧义或配置不完整时，安装会给出可操作的错误；选择规则见[可观测性](../observability/README_zh.md)。

### 网关模式

只有集群运行 Envoy Gateway 时，CLI 才会创建专用的 `GatewayClass` 和 `Gateway`：

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

从当前源码构建 Foretoken 镜像，并配置平台使用这些镜像：

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

重复使用 `--values` 可提供平台镜像、runtime 和硬件配置。发布镜像安装与源码安装模式会记录在 Helm 元数据中，不能静默切换。原本通过 Helm 直接安装的发布实例继续使用原有 Helm 生命周期，CLI 不会自动接管。

## 部署和管理模型服务

部署一个 Kustomize 根目录中渲染出的前端服务和全部模型：

```bash
foretoken deploy examples/multi-model-quickstart
```

该命令会应用配置，在 `FrontendService` 和 `ModelService` 状态变化时输出进度，并在所有资源的当前 generation 就绪后退出。默认等待十分钟，可通过 `--timeout` 调整。

删除同一配置渲染出的资源：

```bash
foretoken delete examples/multi-model-quickstart
```

该命令会等待删除完成，并忽略已经不存在的资源。删除全部 Foretoken 服务后，可以移除平台发布实例：

```bash
foretoken uninstall
```

该命令保留 Foretoken CRD，并在仍有用户服务时拒绝卸载。平台卸载时会一并删除由 CLI 管理的监控和 Gateway 资源，复用的集群组件保持不变。

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
FORETOKEN_FRONTEND_URL="$(foretoken endpoint examples/quickstart)"
FORETOKEN_REQUEST_HOST="$(foretoken endpoint examples/quickstart --host)"
```

Host 值在直接访问时是 URL authority，在 HTTP Gateway 模式下是配置的路由域名。该命令负责等待 LoadBalancer 或 Gateway 地址，服务就绪仍由 `foretoken deploy` 负责。

## 运行评测

使用 pip 安装可选的评测依赖：

```bash
pip install -e '.[bench]'
```

或在已经激活的 uv 虚拟环境中安装评测依赖：

```bash
uv pip install -e '.[bench]'
```

然后运行评测：

```bash
foretoken bench examples/quickstart
```

CLI 使用当前 `kubectl` context，并遵循 `KUBECONFIG` 等标准 Kubernetes 配置。
