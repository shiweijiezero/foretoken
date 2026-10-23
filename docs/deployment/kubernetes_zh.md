<!-- SPDX-License-Identifier: Apache-2.0 -->
<!-- SPDX-FileCopyrightText: Copyright contributors to the Foretoken project -->

# 在 Kubernetes 上部署 Foretoken

[English](kubernetes.md) | [中文](kubernetes_zh.md)

管理员在 K3s、RKE2、KubeSphere、云上 Kubernetes 或其他不属于本地 kind/k3d 指南的集群中安装 Foretoken 时，使用本指南。平台安装后，可以为成员准备[部署命名空间](../../cli/README_zh.md#为成员准备命名空间)，让其使用服务而无需平台管理权限。

## 1. 准备集群

先按所用平台的安装指南创建集群，再在运行 Foretoken 的机器上配置 kubeconfig，并检查当前 context：

```bash
kubectl config current-context
kubectl get nodes
```

集群需要具备：

- 可通过 `kubectl` 和 [Helm 4](../../cli/README_zh.md#开始前)访问；
- 用于平台持久存储的默认 StorageClass；
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
kubectl create namespace foretoken-platform --dry-run=client -o yaml | kubectl apply -f -
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

### 分配外部访问地址

默认的共享 Gateway 需要 LoadBalancer 地址。集群已经提供地址分配时，直接复用已有实现。安装提示 `LoadBalancer support Not verified` 时，可以让 Foretoken 通过 MetalLB 分配节点网段内的空闲地址。先向集群管理员确认可用范围，再添加到 `deploy/platform-values.yaml`，替换下面的示例：

```yaml
loadBalancer:
  managedAddresses:
    - 192.168.1.240-192.168.1.250
```

沿用原安装设置并应用配置：

```bash
foretoken install -e . --registry "$REGISTRY" --values deploy/platform-values.yaml
```

### RDMA 网络

RDMA 用于加速模型实例之间的数据传输。安装时会发现可用设备、复用集群已有的兼容配置，并自动准备受支持的设备分配和网络。常规部署无需在模型配置中指定网卡或端口，主机驱动和物理网络需已可用。

## 3. 部署与更新代码

平台已准备默认模型存储时，直接部署即可。平台尚未准备存储，或需要独立卷时，先[配置动态存储](../features/model-storage_zh.md#使用动态存储)；需要其他共享目录时，按[选择目录](../features/model-storage_zh.md#选择目录)使用管理员已授权的路径。

部署并更新代码：

```bash
foretoken deploy examples/quickstart/qwen3-0.6b -e . --timeout 20m
```

不传 `--name` 时，实例名取自 [`frontend.yaml`](../../examples/quickstart/qwen3-0.6b/frontend.yaml) 中 `FrontendService` 的 `metadata.name`，本例为 `qwen3-0-6b`。实例使用当前 kubeconfig 的命名空间。添加 `--name candidate-a` 则使用 `candidate-a`，后续操作也用这个名字。

修改控制器、前端或模型服务代码后，继续执行同一条部署命令，只更新该实例。共享 CRD 变更需先执行 `foretoken install -e . --registry "$REGISTRY"`，并保留原有安装选项。引擎源码和运行环境设置见[从源码部署 Foretoken](source_zh.md#部署模型服务)。

## 4. 删除 Foretoken

```bash
foretoken delete qwen3-0-6b
foretoken uninstall
```

这些命令只删除 Foretoken 资源，不会删除 Kubernetes 集群。
