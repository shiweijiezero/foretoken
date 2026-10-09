<!-- SPDX-License-Identifier: Apache-2.0 -->
<!-- SPDX-FileCopyrightText: Copyright contributors to the Foretoken project -->

# 使用 kind 部署 Foretoken

[English](kind-deployment.md) | [中文](kind-deployment_zh.md)

kind 适合创建本地 Kubernetes 开发集群。kind 的 Kubernetes 节点运行在容器中，不提供 GPU；单机 GPU 集群请使用[k3d 部署指南](k3d-deployment_zh.md)。

## 1. 安装工具并创建集群

安装 Docker、kind、kubectl、Python 3.11+ 和 Helm，并从 Foretoken 仓库根目录执行：

```bash
pip install -e .
foretoken cluster create kind --name foretoken-dev
kubectl get nodes
```

该命令会创建 kind 集群、刷新 kubeconfig context，并等待控制面就绪。

## 2. 构建并安装 Foretoken

kind 会将源码构建的镜像直接载入节点的 containerd，无需镜像仓库：

```bash
foretoken install -e .
```

快速开始示例需要 GPU；需要运行该示例时请使用[k3d 部署指南](k3d-deployment_zh.md)。kind 集群适合验证平台安装和运行资源需求匹配的 CPU 工作负载。

## 3. 删除集群

```bash
foretoken delete examples/quickstart
foretoken uninstall
foretoken cluster delete kind --name foretoken-dev
```

已有 K3s、RKE2、KubeSphere、云上或其他 Kubernetes 集群，请使用 [Kubernetes 部署指南](kubernetes-deployment_zh.md)。
