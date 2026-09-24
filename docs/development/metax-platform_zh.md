<!--
SPDX-License-Identifier: Apache-2.0
SPDX-FileCopyrightText: Copyright contributors to the Foretoken project
-->

# 准备沐曦 Foretoken 平台

[English](metax-platform.md) | 简体中文

在沐曦 GPU 集群上安装 Foretoken 平台。安装完成后，按[沐曦模型部署指南](../metax-deployment_zh.md)部署和调用模型。

## 开始前

集群需要 Kubernetes 1.29 或更高版本、沐曦驱动和设备插件，并提供 `metax-tech.com/gpu` 资源。准备好目标节点可访问的模型目录，或用于模型缓存的 StorageClass；存储配置见[模型存储](../model-storage_zh.md)。前端还需要可访问的 LoadBalancer 地址；使用网关模式时则需要 Gateway 入口。

先安装 [Foretoken 命令行工具](../../cli/README_zh.md#安装命令行工具)，确认 `kubectl` 指向目标集群。安装平台还需要 Helm 和集群权限；共享依赖由命令行工具按需准备。

## 安装发布版

```bash
foretoken install
```

命令会选择兼容沐曦的发布镜像，安装平台和所需共享依赖，并在平台就绪后返回。网关访问或自定义配置见[命令行安装指南](../../cli/README_zh.md#安装-kubernetes-平台)。

## 从源码安装

需要构建 Foretoken 当前源码时，先按[源码部署指南](../custom-deployment_zh.md)准备构建工具，并从这份源码安装 CLI，再从仓库根目录运行：

```bash
foretoken install -e .
```

该命令使用 Chart 为沐曦选择的推理运行时镜像，构建并安装源码版 model-server。若目标不是本机 kind 或 k3d 集群，请按[源码部署指南](../custom-deployment_zh.md#2-从源码构建镜像并安装平台)登录节点可访问的镜像仓库，并提供 `--registry`；私有仓库还需要镜像拉取 Secret。

### 使用自定义沐曦 SDK 镜像

需要更换 MACA SDK 时，准备与推理运行时兼容的 Ubuntu 24.04 或 Debian 系 SDK 镜像，其中须包含 Python 3.12 和开发头文件。SDK 与驱动版本按[沐曦版本矩阵](https://vllm-metax.readthedocs.io/en/latest/getting_started/quickstart.html)匹配。

从仓库根目录构建推理运行时，将 `<maca-sdk-image>` 替换为实际镜像：

```bash
METAX_SDK_IMAGE=<maca-sdk-image> \
VLLM_METAX_IMAGE=foretoken-vllm-metax:custom \
make image-vllm-metax
```

如需更换受支持的引擎版本，在构建命令中设置 `VLLM_METAX_VERSION`，并使用与之兼容的 SDK 镜像。将以下配置保存为 `metax-values.yaml`；如果已有兼容的推理运行时镜像，也可跳过构建，直接将镜像地址填在此处：

```yaml
runtime:
  vllm:
    image: foretoken-vllm-metax:custom
```

```bash
foretoken install -e . --values metax-values.yaml
```

源码安装会在所选推理镜像上构建 Foretoken model-server，再分发 Foretoken 镜像并安装平台。远程集群按前述方法添加 `--registry`。如需手动导入镜像或操作 Helm，见[源码镜像手工流程](source-image-lifecycle_zh.md)。

## 卸载

先删除模型部署，再运行：

```bash
foretoken uninstall
```

CRD 和复用的集群资源会保留。
