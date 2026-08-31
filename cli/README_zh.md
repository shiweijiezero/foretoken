<!--
SPDX-License-Identifier: Apache-2.0
SPDX-FileCopyrightText: Copyright contributors to the Foretoken project
-->

# Foretoken CLI

[English](README.md) | 简体中文

Foretoken CLI 通过统一的 `foretoken` 入口部署 Kustomize 配置、查看服务就绪状态、解析前端访问入口并运行评测。

使用 pip 或 uv 安装 Foretoken CLI：

```bash
pip install -e .
# 或
uv pip install -e .
```

部署一个 Kustomize 根目录中渲染出的前端服务和全部模型：

```bash
foretoken deploy examples/multi-model-quickstart
```

该命令会应用配置，在 `FrontendService` 和 `ModelService` 状态变化时输出进度，并在所有资源的当前 generation 就绪后退出。默认等待十分钟，可通过 `--timeout` 调整。

删除同一配置渲染出的资源：

```bash
foretoken delete examples/multi-model-quickstart
```

该命令会等待删除完成，并忽略已经不存在的资源。

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

评测能力使用可选依赖：

```bash
pip install -e '.[bench]'
# 或
uv pip install -e '.[bench]'

foretoken bench examples/quickstart
```

CLI 使用当前 `kubectl` context，并遵循 `KUBECONFIG` 等标准 Kubernetes 配置。
