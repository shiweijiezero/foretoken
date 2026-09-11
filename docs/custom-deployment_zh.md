<!-- SPDX-License-Identifier: Apache-2.0 -->
<!-- SPDX-FileCopyrightText: Copyright contributors to the Foretoken project -->

# 从源码部署 Foretoken

[English](custom-deployment.md) | [中文](custom-deployment_zh.md)

本指南介绍如何从源码构建 Foretoken 镜像、配置 Kubernetes 平台使用这些镜像，以及修改源码后如何重新部署。模型服务仍通过 `foretoken deploy` 单独部署。

准备好 Python 3.11+、Git、启用 BuildKit 的 Docker、Make、kubectl、Helm 和由 rustup 管理的 Rust 工具链。获取当前源码，并从仓库根目录执行命令：

```bash
git clone https://github.com/shiweijiezero/foretoken.git
cd foretoken
```

## 1. 准备目标 Kubernetes 集群

确认 `kubectl` 当前指向目标集群：

```bash
kubectl config current-context
kubectl get nodes
```

## 2. 从源码构建镜像并安装平台

使用 pip 从源码根目录安装命令行工具：

```bash
pip install -e .
```

或使用 uv 创建并激活虚拟环境后安装：

```bash
uv venv
source .venv/bin/activate
uv pip install -e .
```

本地 kind 或 k3d 集群可以直接构建并导入镜像：

```bash
foretoken install -e .
```

其他集群需要使用所有目标节点都能访问的镜像仓库。将 `example` 替换为有推送权限的命名空间：

```bash
export REGISTRY=ghcr.io/example/foretoken
docker login ghcr.io
foretoken install -e . --registry "$REGISTRY"
```

使用私有镜像仓库时，在平台命名空间和每个工作负载命名空间中创建同名的镜像拉取 Secret，并将以下配置保存为 `platform-values.yaml`：

```yaml
imagePullSecrets:
  - name: registry-auth
workload:
  imagePullSecrets:
    - name: registry-auth
```

```bash
foretoken install -e . \
  --registry "$REGISTRY" \
  --values platform-values.yaml
```

## 3. 确认平台部署完成

`foretoken install -e .` 会等待 Helm release 和控制平面滚动更新完成。命令成功退出后，检查已安装的 release 和控制器：

```bash
helm status foretoken --namespace foretoken-platform
kubectl get deployment foretoken-control-plane \
  --namespace foretoken-platform
```

Deployment 应显示所有期望副本均已 Ready。模型工作负载只会在下一步部署后出现。

## 4. 部署快速开始示例（可选）

快速开始工作负载请求 1 张 GPU、8 个 CPU 和 52 GiB 内存；还需为平台预留额外容量。使用 k3d 时，先按[使用 k3d 部署 Foretoken](k3d-deployment_zh.md)完成 GPU 配置，并确认当前 Kubernetes 上下文指向目标 k3d 集群。

准备好 `examples/quickstart/cache.yaml` 声明的[模型存储](model-storage_zh.md)，再从仓库根目录部署：

```bash
foretoken deploy examples/quickstart --timeout 20m
```

该命令会发现渲染后的服务、输出状态变化，并在当前配置就绪后退出。

## 5. 发送请求（可选）

完成[第 4 节：部署快速开始示例](#4-部署快速开始示例可选)后，解析默认 `local` 前端模式的 URL，并发送 OpenAI API 兼容格式的请求：

```bash
FRONTEND_URL="$(foretoken endpoint examples/quickstart)"

curl --fail-with-body "$FRONTEND_URL/v1/chat/completions" \
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

修改代码后再次执行同一条源码安装命令：

```bash
foretoken install -e .
```

远程集群继续使用同一个镜像仓库：

```bash
foretoken install -e . --registry "$REGISTRY"
```

BuildKit 会复用编译缓存。命令只导入或推送发生变化的镜像，保持源码安装模式，并滚动更新使用本地同名镜像且内容发生变化的工作负载。需要排查底层镜像或 Helm 操作时，参阅维护者[源码镜像手工生命周期](development/source-image-lifecycle_zh.md)。
