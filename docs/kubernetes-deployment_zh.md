<!-- SPDX-License-Identifier: Apache-2.0 -->
<!-- SPDX-FileCopyrightText: Copyright contributors to the Foretoken project -->

# 在 Kubernetes 上部署 Foretoken

[English](kubernetes-deployment.md) | [中文](kubernetes-deployment_zh.md)

K3s、RKE2、KubeSphere、云上 Kubernetes 或其他不属于本地 kind/k3d 指南的集群，使用本指南。

## 1. 准备集群

先按所用平台的安装指南创建集群，再在运行 Foretoken 的机器上配置 kubeconfig，并检查当前 context：

```bash
kubectl config current-context
kubectl get nodes
```

集群需要具备：

- 可通过 `kubectl` 和 Helm 访问；
- 用于保存编译缓存的默认 StorageClass；
- 部署 GPU 模型时，节点已安装 GPU 驱动和对应设备插件；
- 使用源码构建镜像时，构建 Pod 和所有节点都能访问同一个镜像仓库。

K3s 单 server 示例：

```bash
curl -sfL https://get.k3s.io | sh -
mkdir -p "$HOME/.kube"
sudo cp /etc/rancher/k3s/k3s.yaml "$HOME/.kube/config"
sudo chown "$(id -u):$(id -g)" "$HOME/.kube/config"
export KUBECONFIG="$HOME/.kube/config"
kubectl get nodes
```

RKE2、KubeSphere 和云 Kubernetes 请按对应平台配置集群及 kubeconfig，完成后继续本指南。

## 2. 从源码安装 Foretoken

从 Foretoken 仓库根目录执行：

```bash
pip install -e .
export REGISTRY=registry.example.com:5000/foretoken
foretoken install -e . --registry "$REGISTRY"
```

内网环境优先使用无认证仓库。如果仓库需要认证，安装前先登录，并在每个工作负载命名空间创建 `registry-auth`：

```bash
docker login registry.example.com:5000
kubectl create secret generic registry-auth \
  --namespace foretoken-platform \
  --from-file=.dockerconfigjson="$HOME/.docker/config.json" \
  --type=kubernetes.io/dockerconfigjson
```

在每个工作负载命名空间创建同名 Secret，并在 `deploy/platform-values.yaml` 中保存引用：

```yaml
imagePullSecrets:
  - name: registry-auth
workload:
  imagePullSecrets:
    - name: registry-auth
```

使用认证仓库时，通过 `--values deploy/platform-values.yaml` 安装。

## 3. 部署与更新代码

快速开始示例在本地 k3d 使用仓库中的 `data/` 目录；在远程集群上，Foretoken 会自动为这个相对目录创建动态 PVC，因此无需修改示例 YAML。多节点集群使用 `ReadWriteMany`，单节点集群会自动使用 `ReadWriteOnce`。集群需要其他存储策略时，在 `cache.yaml` 中设置 `initialSize`、`storageClassName`、`accessMode` 或 `maxSize`。

部署并更新代码：

```bash
foretoken deploy examples/quickstart --timeout 20m
```

修改源码后，继续执行同一条命令。源码改动在 BuildKit Pod 中编译并发布到工作负载；普通 Python、Triton、Rust、CUDA 和 C/C++ 改动会复用运行时镜像。引擎源码和运行环境设置见[从源码部署 Foretoken](custom-deployment_zh.md#部署与更新代码)。

## 4. 删除 Foretoken

```bash
foretoken delete examples/quickstart
foretoken uninstall
```

这些命令只删除 Foretoken 资源，不会删除 Kubernetes 集群。
