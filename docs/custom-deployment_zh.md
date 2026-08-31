<!-- SPDX-License-Identifier: Apache-2.0 -->
<!-- SPDX-FileCopyrightText: Copyright contributors to the Foretoken project -->

# 从源码部署 Foretoken

[English](custom-deployment.md) | [中文](custom-deployment_zh.md)

本指南介绍如何从源码构建并部署 Foretoken，以及修改源码后如何重新部署。

## 1. 准备目标 Kubernetes 集群

确认 `kubectl` 当前指向目标集群：

```bash
kubectl config current-context
kubectl get nodes
```

## 2. 构建并部署源码

Kubernetes 节点需要能够获得源码构建出的镜像。可以直接导入本地镜像，也可以通过 OCI 镜像仓库分发。

### 2.1 直接导入本地镜像

#### 2.1.1 导入本地镜像

**选项 1：导入 Kind 集群。** 使用 Kind 验证控制平面、CRD、前端服务和调度逻辑时，可以直接创建集群。需要运行 GPU 模型服务时，使用选项 2 的 k3d，并按 [使用 k3d 部署 Foretoken](k3d-deployment_zh.md) 指定可用 GPU。先安装 Kind：

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
# 预计执行时间：约 20 秒
export KIND_CLUSTER=foretoken-local
kind create cluster --name "$KIND_CLUSTER"
```

若需要在同一台机器上模拟多节点拓扑，使用项目提供的 Kind 配置文件。

```bash
# 预计执行时间：约 30 秒
export KIND_CLUSTER=foretoken-local
kind create cluster \
  --name "$KIND_CLUSTER" \
  --config deploy/kind/multi-node.yaml
```

创建集群后，构建并导入本地镜像。

```bash
# 预计执行时间：约 8 分钟
make dev-build

kind load docker-image \
  --name "$KIND_CLUSTER" \
  foretoken-dev-control-plane:latest \
  foretoken-dev-frontend:latest \
  foretoken-dev-model-server:latest

mkdir -p ./tmp
kind get kubeconfig --name "$KIND_CLUSTER" \
  > "./tmp/kubeconfig-$KIND_CLUSTER.yaml"
export KUBECONFIG="$PWD/tmp/kubeconfig-$KIND_CLUSTER.yaml"
kubectl get nodes
```

**选项 2：导入 k3d 集群。** 先查看当前机器上的集群，并将 `CLUSTER` 设置为实际名称：

```bash
k3d cluster list
export CLUSTER=your-cluster-name
```

如果目标集群尚未创建，请先完成[使用 k3d 部署 Foretoken](k3d-deployment_zh.md)中的集群创建步骤。然后在仓库根目录构建并导入本地镜像。

```bash
# 预计执行时间：约 6 分钟
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

**选项 3：导入单节点上的 containerd。** Kubernetes 节点与开发机是同一台机器时，在仓库根目录构建镜像包并导入 Kubernetes 使用的 containerd 镜像命名空间。

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

**选项 4：导入多节点 containerd。** 针对使用 containerd 的离线多节点 Kubernetes 集群，在开发机上构建镜像包。

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

#### 2.1.2 安装 Foretoken 平台

镜像导入完成后，确认当前 Kubernetes 上下文指向目标集群，然后执行 Helm 命令。

```bash
# 预计执行时间：约 30 秒
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

### 2.2 通过 OCI 镜像仓库构建并部署

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

## 3. 确认平台部署完成

脚本会通过 Helm 安装或更新 Foretoken，并等待控制平面完成滚动更新。命令成功退出并显示以下输出后，平台部署完成：

```text
Foretoken deployment completed.
Changed images: control-plane=false frontend=true model-server=false
```

## 4. 部署快速开始示例（可选）

快速开始示例需要目标 Kubernetes 集群提供 GPU 资源。使用 k3d 时，先按[使用 k3d 部署 Foretoken](k3d-deployment_zh.md)完成 GPU 配置，并确认当前 Kubernetes 上下文指向目标 k3d 集群。

需要启动示例前端服务和 `Qwen/Qwen3-0.6B` 模型服务时，从仓库根目录安装 CLI 并部署。

```bash
pip install -e .
foretoken deploy examples/quickstart --timeout 6m
```

该命令会发现渲染后的服务、输出状态变化，并在当前配置就绪后退出。

## 5. 发送请求（可选）

完成[第 4 节：部署快速开始示例](#4-部署快速开始示例可选)后，解析默认 `local` 前端模式的 URL：

```bash
FRONTEND_URL="$(foretoken endpoint examples/quickstart)"
```

先确认前端服务和模型路由可用：

```bash
curl --fail "$FRONTEND_URL/healthz"
curl --fail "$FRONTEND_URL/v1/models"
```

`/healthz` 成功，并且 `/v1/models` 返回 `Qwen/Qwen3-0.6B` 后，发送 OpenAI API 兼容格式的请求：

```bash
curl "$FRONTEND_URL/v1/chat/completions" \
  -H 'Content-Type: application/json' \
  -d '{
    "model": "Qwen/Qwen3-0.6B",
    "messages": [{"role": "user", "content": "Reply with: Foretoken is ready"}],
    "max_tokens": 32,
    "temperature": 0
  }'
printf '\n'
```

## 6. 修改源码后重新部署

直接导入本地镜像时，修改源码后运行。

```bash
make dev-deploy
```

使用镜像仓库时运行：

```bash
REGISTRY="$REGISTRY" make dev-deploy
```

BuildKit 会复用编译缓存。脚本只导入或推送构建结果发生变化的镜像，并只滚动更新对应的工作负载。看到 `Foretoken deployment completed.` 后，平台更新完成。
