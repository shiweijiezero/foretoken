<!--
SPDX-License-Identifier: Apache-2.0
SPDX-FileCopyrightText: Copyright contributors to the Foretoken project
-->

# Foretoken CLI

[English](README.md) | 简体中文

Foretoken CLI 通过统一的 `foretoken` 入口安装平台、部署 Kustomize 配置、查看服务就绪状态、解析前端访问入口并运行评测。

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

这一步只会在当前 Python 环境中安装 `foretoken` 命令，不会修改 Kubernetes 集群。CLI 会安装与自身软件包版本一致的 Foretoken Chart；运行 `foretoken --version` 可以查看该发布版本。

使用 CLI 将平台安装到当前 Kubernetes context，并采用默认的本地访问模式。CLI 管理的平台资源固定使用 `foretoken-platform` 命名空间；以后再次运行同一命令会更新现有安装：

```bash
foretoken install
```

命令会复用一个兼容的 Prometheus；如果没有，则安装由 CLI 管理的监控栈。发现 NVIDIA GPU 节点时，命令会复用一个已就绪且 ServiceMonitor 有效的 DCGM Exporter；没有 exporter 时才安装由 CLI 管理的版本。共享 Prometheus 必须能够选择该 ServiceMonitor。已有 exporter 重复、不健康、覆盖不全或无法采集时会停止安装，不会继续叠加。在沐曦 GPU 节点上，CLI 要求并复用一个由平台管理、ServiceMonitor 有效的 mxExporter。CLI 不安装 GPU 驱动、device plugin 或厂商 Operator。只有自动发现得到多个兼容实例时，才需要使用 `--prometheus NAMESPACE/NAME` 指定。

网关模式要求集群已经安装 Gateway Controller。未提供已有 Gateway 信息时，CLI 会创建专用的 `GatewayClass` 和 `Gateway`：

```bash
foretoken install --frontend-mode gateway
```

如需复用已有 Gateway：

```bash
foretoken install \
  --frontend-mode gateway \
  --gateway-name inference-gateway \
  --gateway-namespace gateway-system \
  --gateway-section-name https
```

使用 `--dry-run` 可在不修改集群的情况下验证并查看安装计划。重复使用 `--values` 可提供平台镜像、runtime 和硬件配置。原本通过 Helm 直接安装的发布实例继续使用原有 Helm 生命周期，CLI 不会自动接管。

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

该命令保留 Foretoken CRD，并在仍有用户服务时拒绝卸载。

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
