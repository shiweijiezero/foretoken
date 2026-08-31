<!--
SPDX-License-Identifier: Apache-2.0
SPDX-FileCopyrightText: Copyright contributors to the Foretoken project
-->

# Foretoken CLI

[English](README.md) | 简体中文

Foretoken CLI 通过统一的 `foretoken` 入口部署 Kustomize 配置、查看服务就绪状态并运行评测。

从仓库根目录安装部署和状态命令：

```bash
pip install -e .
```

部署一个 Kustomize 根目录中渲染出的前端服务和全部模型：

```bash
foretoken deploy -k examples/multi-model-quickstart
```

该命令会应用配置，在 `FrontendService` 和 `ModelService` 状态变化时输出进度，并在所有资源的当前 generation 就绪后退出。默认等待十分钟，可通过 `--timeout` 调整。

不应用配置，直接查看同一部署的状态：

```bash
foretoken status -k examples/multi-model-quickstart
```

查看一个命名空间中的全部 Foretoken 服务，或持续观察状态变化：

```bash
foretoken status -n foretoken-multi-model-demo
foretoken status -n foretoken-multi-model-demo --watch
```

评测能力使用可选依赖：

```bash
pip install -e '.[bench]'
foretoken bench --deploy examples/quickstart
```

CLI 使用当前 `kubectl` context，并遵循 `KUBECONFIG` 等标准 Kubernetes 配置。
