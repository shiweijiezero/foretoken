<!-- SPDX-License-Identifier: Apache-2.0 -->
<!-- SPDX-FileCopyrightText: Copyright contributors to the Foretoken project -->

# 源码更新与镜像生命周期

[English](source-image-lifecycle.md) | [中文](source-image-lifecycle_zh.md)

本维护者指南说明运行时源码启用、手工镜像导入和原始 Helm 操作。日常安装与重新部署使用[源码部署指南](../custom-deployment_zh.md)。除非另有说明，命令均从 Foretoken 仓库根目录执行。

沐曦镜像准备方式见[准备沐曦 Foretoken 平台](metax-platform_zh.md#从源码安装)。统一发布两个 GPU 变体及 Chart，使用[发布产物命令](release_zh.md#构建与推送发布产物)。

## 启用运行时源码

CLI 在工作站保存源码目录和安装设置，并将其关联到已安装的平台与集群。部署时，CLI 将构建输入与上次准备的源码比较。Rust 改动通过 Dockerfile 的 `source-export` 阶段复用 BuildKit 缓存构建；Python 适配代码改动则替换整个适配目录。每个组件的完整代码包会保留此前准备的可执行文件，因此后续仅修改 Python 时，之前的 Rust 改动仍然有效。

CLI 先将完整代码包发布到部署的持久运行时缓存，再应用服务配置。已有控制器负责前端滚动更新和模型 Pool/Group 替换，包括分布式 worker。进程启动时，镜像中的引导代码选择代码包内的可执行文件和 Python 适配代码。发布由独立的 CPU Pod 完成，旧推理进程无法启动时也能准备新代码包。

引导代码自身、依赖、构建配置、控制面代码和 Helm 的改动，由平台安装流程准备镜像；部署没有可用的持久运行时存储时也使用该流程。CLI 源码安装成功后会清除服务的源码选择，因为镜像已包含当前源码。源码未变化时复用已准备的代码包；期望配置未变化时，控制器保留现有工作负载。

运行时代码包适用于前端和 vLLM model-server。vLLM-Omni 使用独立镜像构建目标，见 [vLLM-Omni 配方](../../examples/recipes/minimax-h3/a100-bf16-tp2/README_zh.md)。厂商引擎的 Python 和 CUDA 源码属于推理引擎镜像，不进入运行时代码包。

## 直接导入本地镜像

选项 1：导入 Kind 集群。使用 Kind 验证控制平面、CRD、前端服务和调度逻辑时，可以直接创建集群。需要运行 GPU 模型服务时，使用选项 2 的 k3d，并按 [使用 k3d 部署 Foretoken](../k3d-deployment_zh.md) 指定可用 GPU。先安装 Kind：

```bash
export KIND_VERSION=v0.32.0
mkdir -p ./tmp/bin
curl -fL \
  -o ./tmp/bin/kind \
  "https://github.com/kubernetes-sigs/kind/releases/download/$KIND_VERSION/kind-linux-amd64"
chmod +x ./tmp/bin/kind
export PATH="$PWD/tmp/bin:$PATH"
kind version
```

创建单节点集群：

```bash
export KIND_CLUSTER=foretoken-local
kind create cluster --name "$KIND_CLUSTER"
```

若需要在同一台机器上模拟多节点拓扑，使用项目提供的 Kind 配置文件。

```bash
export KIND_CLUSTER=foretoken-local
kind create cluster \
  --name "$KIND_CLUSTER" \
  --config deploy/kind/multi-node.yaml
```

创建集群后，构建并导入本地镜像。vLLM adapter 支持 vLLM 0.20–0.29 及当前 0.30 开发版的 EngineCore 协议布局；引擎与模型本身还需匹配目标加速器。inference-engine image 通常通过 `python` 提供 Python 解释器；如果必须使用特定解释器路径，同时设置两个构建输入：

```bash
INFERENCE_ENGINE_IMAGE=<compatible-inference-engine-image> \
FORETOKEN_VLLM_PYTHON=/absolute/path/to/python \
make dev-build
```

否则直接使用默认值：

```bash
make dev-build

kind load docker-image \
  --name "$KIND_CLUSTER" \
  foretoken-dev-control-plane:latest \
  foretoken-dev-frontend:latest \
  foretoken-dev-model-server:latest

kubectl config use-context "kind-$KIND_CLUSTER"
kubectl get nodes
```

选项 2：导入 k3d 集群。先查看当前机器上的集群，并将 `CLUSTER` 设置为实际名称：

```bash
k3d cluster list
export CLUSTER=your-cluster-name
```

如果目标集群尚未创建，请先完成[使用 k3d 部署 Foretoken](../k3d-deployment_zh.md)中的集群创建步骤。然后在仓库根目录构建并导入本地镜像。

```bash
make dev-build

k3d image import --cluster "$CLUSTER" \
  foretoken-dev-control-plane:latest \
  foretoken-dev-frontend:latest \
  foretoken-dev-model-server:latest

mkdir -p ./tmp
k3d kubeconfig get "$CLUSTER" \
  > "./tmp/kubeconfig-$CLUSTER.yaml"
export KUBECONFIG="$PWD/tmp/kubeconfig-$CLUSTER.yaml"
kubectl get nodes
```

`--namespace k8s.io` 表示 Kubernetes 使用的 containerd 镜像命名空间。选项 3 和选项 4 由节点管理员执行。

选项 3：导入单节点上的 containerd。Kubernetes 节点与开发机是同一台机器时，在仓库根目录构建镜像包并导入 Kubernetes 使用的 containerd 镜像命名空间。

```bash
make dev-build
mkdir -p ./tmp

docker save \
  foretoken-dev-control-plane:latest \
  foretoken-dev-frontend:latest \
  foretoken-dev-model-server:latest \
  --output ./tmp/foretoken-dev-images.tar

sudo ctr --namespace k8s.io images import ./tmp/foretoken-dev-images.tar
rm ./tmp/foretoken-dev-images.tar
```

选项 4：导入多节点 containerd。针对使用 containerd 的离线多节点 Kubernetes 集群，在开发机上构建镜像包。

```bash
make dev-build
mkdir -p ./tmp

docker save \
  foretoken-dev-control-plane:latest \
  foretoken-dev-frontend:latest \
  foretoken-dev-model-server:latest \
  --output ./tmp/foretoken-dev-images.tar
```

将 `node-a` 和 `node-b` 替换为实际节点的 SSH 地址，然后导入每个可能运行 Foretoken 工作负载的节点。

```bash
for NODE in node-a node-b; do
  # 将镜像包传输到节点
  ssh "$NODE" 'mkdir -p ./tmp'
  rsync --archive --progress \
    ./tmp/foretoken-dev-images.tar \
    "$NODE:./tmp/foretoken-dev-images.tar"

  # 在节点上导入 Kubernetes 使用的 containerd 镜像空间
  ssh -t "$NODE" \
    'sudo ctr --namespace k8s.io images import ./tmp/foretoken-dev-images.tar &&
     rm ./tmp/foretoken-dev-images.tar'
done
```

## 使用 Helm 安装平台

镜像导入完成后，确认当前 Kubernetes 上下文指向目标集群，然后执行 Helm 命令。

```bash
helm upgrade --install foretoken \
  ./deploy/charts/foretoken \
  --namespace foretoken-platform \
  --create-namespace \
  --set frontend.enabled=true \
  --set frontend.mode=local \
  --set image.repository=foretoken-dev-control-plane \
  --set image.tag=latest \
  --set image.pullPolicy=Never \
  --set frontend.image=foretoken-dev-frontend:latest \
  --set runtime.vllm.image=foretoken-dev-model-server:latest \
  --wait \
  --timeout=5m
```

## 通过 OCI 镜像仓库构建并部署

OCI 镜像仓库可以将开发机构建的镜像分发给 Kubernetes 节点。以下示例使用 GHCR。

```bash
export GITHUB_USER=your-github-user
export REGISTRY="ghcr.io/$GITHUB_USER/foretoken-dev"
docker login ghcr.io
REGISTRY="$REGISTRY" make dev-deploy
```

该命令会推送镜像并安装或更新 Foretoken 平台。

脚本会自动推送：

```text
ghcr.io/your-github-user/foretoken-dev/control-plane:<tag>
ghcr.io/your-github-user/foretoken-dev/frontend:<tag>
ghcr.io/your-github-user/foretoken-dev/model-server:<tag>
```

使用私有镜像仓库时，通过 `IMAGE_PULL_SECRET` 提供 Kubernetes 镜像拉取 Secret：

```bash
REGISTRY="$REGISTRY" \
IMAGE_PULL_SECRET=foretoken-registry \
make dev-deploy
```
