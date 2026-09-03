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

使用 pip 从源码根目录安装 CLI：

```bash
pip install -e .
```

或使用 uv 创建并激活虚拟环境后安装：

```bash
uv venv
source .venv/bin/activate
uv pip install -e .
```

将当前源码构建并安装到当前 Kubernetes context：

```bash
foretoken install -e .
```

当前 context 是标准 kind 或 k3d 时，命令会构建并导入本地镜像。其他 Kubernetes context 需要提供所有目标节点都能访问的 registry：

```bash
foretoken install -e . --registry ghcr.io/example/foretoken
```

私有 registry 还需要在平台命名空间和每个 workload 命名空间中创建同名 pull Secret，并通过 values 文件引用：

```yaml
imagePullSecrets:
  - name: registry-auth
workload:
  imagePullSecrets:
    - name: registry-auth
```

```bash
foretoken install -e . \
  --registry registry.example.com/foretoken \
  --values platform-values.yaml
```

命令会复用仓库已有的构建、导入和推送生命周期，再执行与发布镜像相同的平台和可观测性安装。这是源码安装的唯一权威路径。下面的可选章节仅用于排查本地镜像导入，不定义第二套平台生命周期。

### 2.1 检查本地镜像导入

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

**选项 2：导入 k3d 集群。** 先查看当前机器上的集群。以下示例使用 k3d 部署指南创建的 `foretoken-qwen-test` 集群：

```bash
k3d cluster list
export CLUSTER=foretoken-qwen-test
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

## 3. 部署快速开始示例（可选）

快速开始示例需要目标 Kubernetes 集群提供 GPU 资源。使用 k3d 时，先按[使用 k3d 部署 Foretoken](k3d-deployment_zh.md)完成 GPU 配置，并确认当前 Kubernetes 上下文指向目标 k3d 集群。

需要启动示例前端服务和 `Qwen/Qwen3-0.6B` 模型服务时，使用第 2 节已安装的 CLI 从仓库根目录部署：

```bash
foretoken deploy examples/quickstart --timeout 6m
```

该命令会发现渲染后的服务、输出状态变化，并在当前配置就绪后退出。

## 4. 发送请求（可选）

完成[第 3 节：部署快速开始示例](#3-部署快速开始示例可选)后，解析默认 `local` 前端模式的 URL：

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

## 5. 修改源码后重新部署

修改代码后再次执行同一条源码安装命令：

```bash
foretoken install -e .
```

远程集群继续使用同一个镜像仓库：

```bash
foretoken install -e . --registry ghcr.io/example/foretoken
```

BuildKit 会复用编译缓存。命令只导入或推送发生变化的镜像，保持源码安装模式，并滚动更新使用本地同名镜像且内容发生变化的工作负载。
