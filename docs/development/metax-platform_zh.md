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

构建会自动从公开 SDK 软件包和固定版本源码准备沐曦推理运行时，包含 GLM-5.3 支持。镜像如何导入集群或通过仓库分发，见[源码部署指南](../custom-deployment_zh.md#2-从源码构建镜像并安装平台)。

需要使用自己的 SDK 镜像时，为命令设置 `METAX_SDK_IMAGE`。若要复用已有推理运行时而非重新构建，在通过 `--values` 传入的平台配置中设置 `runtime.vllm.image`。

## 卸载

先删除模型部署，再运行：

```bash
foretoken uninstall
```

CRD 和复用的集群资源会保留。
