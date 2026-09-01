<!-- SPDX-License-Identifier: Apache-2.0 -->
<!-- SPDX-FileCopyrightText: Copyright contributors to the Foretoken project -->

# 使用 k3d 部署 Foretoken

[English](k3d-deployment.md) | [中文](k3d-deployment_zh.md)

k3d 在 Docker 容器中运行轻量级 Kubernetes 发行版 k3s。它适合在一台共享 GPU 服务器上创建相互隔离、可随时删除的 Foretoken 集群，同时继续使用标准 Helm、CRD 和 Kubernetes API。k3d 集群的节点位于同一台 Docker 主机；跨物理机器部署使用 k3s 或 Kubernetes。

创建 k3d 集群后，在仓库根目录运行 `make dev-deploy`。脚本会构建源码并将发生变化的本地镜像导入当前集群；后续的 Helm 部署、等待就绪和请求验证与其他 Kubernetes 集群相同。

## k3d 如何限定物理 GPU

标准 Kubernetes Pod 请求的是 GPU 类型和数量：

```yaml
resources:
  limits:
    nvidia.com/gpu: 1
```

Pod 不指定宿主机 GPU 编号。k3d 可以在创建 Kubernetes 节点容器时先限制 Docker 可见设备：

```text
宿主机物理 GPU 6、7
→ Docker --gpus '"device=6,7"'
→ k3d 节点容器
→ k3s NVIDIA 设备插件
→ Foretoken Pod
```

## 前置条件

主机需要：

- Linux；
- NVIDIA 驱动程序；
- NVIDIA Container Toolkit；
- 可使用 NVIDIA 运行时的 Docker；
- k3d、kubectl 和 Helm。

## 1. 选择 GPU 并命名集群

查看 GPU：

```bash
nvidia-smi
```

以下示例选择 GPU 6、7，并将集群命名为 `foretoken-qwen-test`：

```bash
export GPU_INDICES=6,7
export CLUSTER=foretoken-qwen-test
```

## 2. 创建限定 GPU 的 k3d 集群

下面的 Bash 代码读取 NVIDIA 运行时、配置和依赖库的位置，并为 k3d 生成挂载参数：

```bash
declare -a K3D_VOLUME_ARGS=()
declare -A K3D_MOUNTED_PATHS=()

add_k3d_mount() {
  local path="$1"
  [ -e "$path" ] || return 0
  [ -z "${K3D_MOUNTED_PATHS[$path]+x}" ] || return 0
  K3D_MOUNTED_PATHS["$path"]=1
  K3D_VOLUME_ARGS+=(--volume "$path:$path@server:0")
}

for NAME in \
  nvidia-container-runtime \
  nvidia-container-runtime-hook \
  nvidia-container-cli \
  nvidia-ctk; do
  TOOL_PATH="$(command -v "$NAME")"
  add_k3d_mount "$TOOL_PATH"

  while read -r PATH_KIND LIBRARY_PATH; do
    if [ "$PATH_KIND" = directory ]; then
      add_k3d_mount "$(realpath -m "$(dirname "$LIBRARY_PATH")")"
    else
      add_k3d_mount "$LIBRARY_PATH"
    fi
  done < <(
    ldd "$TOOL_PATH" |
      awk '
        $2 == "=>" && $3 ~ /^\// { print "directory", $3 }
        $1 ~ /^\// { print "file", $1 }
      '
  )
done

for CONFIG_DIR in \
  /etc/nvidia-container-runtime \
  /usr/local/etc/nvidia-container-runtime; do
  add_k3d_mount "$CONFIG_DIR"
done

for LDCONFIG_PATH in \
  "$(command -v ldconfig)" \
  /sbin/ldconfig.real \
  /usr/sbin/ldconfig.real; do
  add_k3d_mount "$LDCONFIG_PATH"
done
```

创建包含单个 server 节点的集群：

```bash
if k3d cluster get "$CLUSTER" >/dev/null 2>&1; then
  k3d cluster delete "$CLUSTER"
fi

k3d cluster create "$CLUSTER" \
  --config deploy/k3d/config.yaml \
  --gpus "\"device=$GPU_INDICES\"" \
  "${K3D_VOLUME_ARGS[@]}"
```

查看创建后的节点：

```bash
kubectl get nodes
```

## 3. 安装 NVIDIA 设备插件

```bash
kubectl apply -f \
  https://raw.githubusercontent.com/NVIDIA/k8s-device-plugin/v0.17.4/deployments/static/nvidia-device-plugin.yml
```

内层 NVIDIA 运行时使用与外层 k3d 相同的宿主机 GPU 列表：

```bash
kubectl set env daemonset/nvidia-device-plugin-daemonset \
  --namespace kube-system \
  NVIDIA_VISIBLE_DEVICES="$GPU_INDICES"

kubectl rollout status daemonset/nvidia-device-plugin-daemonset \
  --namespace kube-system \
  --timeout=3m
```

## 4. 安装并访问 Foretoken

进入 Foretoken 项目路径：

```bash
cd /path/to/your/foretoken
```

### 4.1 选择部署方式

- **使用发布镜像**：继续执行 [第 4.2 节：本地模式](#42-本地模式) 或 [第 4.3 节：网关模式](#43-网关模式)。
- **从源码部署**：依次完成[第 2.1 节：直接导入本地镜像](custom-deployment_zh.md#21-直接导入本地镜像)、[第 4 节：部署快速开始示例](custom-deployment_zh.md#4-部署快速开始示例可选)和[第 5 节：发送请求](custom-deployment_zh.md#5-发送请求可选)。

使用发布镜像部署时，先从仓库根目录安装一次 CLI：

```bash
pip install -e .
```

### 4.2 本地模式

使用发布镜像安装 Foretoken 并部署快速开始示例：

```bash
foretoken install

foretoken deploy examples/quickstart --timeout 6m
```

解析 k3s ServiceLB 为前端服务分配的地址：

```bash
FORETOKEN_FRONTEND_URL="$(foretoken endpoint examples/quickstart)"
FORETOKEN_REQUEST_HOST="$(foretoken endpoint examples/quickstart --host)"
```

### 4.3 网关模式

先在 `examples/quickstart/frontend.yaml` 中设置对外域名：

```yaml
spec:
  hostname: foretoken.example.com
```

网关模式要求集群已经安装 Gateway Controller。如果集群尚未安装，可以使用以下命令安装 Envoy Gateway：

```bash
helm upgrade --install envoy-gateway \
  oci://docker.io/envoyproxy/gateway-helm \
  --namespace envoy-gateway-system \
  --create-namespace \
  --wait
```

创建 Foretoken 专用 Gateway，并使用发布镜像部署快速开始示例：

```bash
foretoken install --frontend-mode gateway
foretoken deploy examples/quickstart --timeout 6m
```

解析已配置的 Gateway 入口：

```bash
FORETOKEN_FRONTEND_URL="$(foretoken endpoint examples/quickstart)"
FORETOKEN_REQUEST_HOST="$(foretoken endpoint examples/quickstart --host)"
```

### 4.4 发送 OpenAI API 兼容格式的请求

```bash
curl "$FORETOKEN_FRONTEND_URL/v1/chat/completions" \
  -H "Host: $FORETOKEN_REQUEST_HOST" \
  -H 'Content-Type: application/json' \
  -d '{
    "model": "Qwen/Qwen3-0.6B",
    "messages": [{"role": "user", "content": "Reply with: Foretoken is ready"}],
    "max_tokens": 32,
    "temperature": 0
  }'
printf '\n'
```

Foretoken 的 YAML 仍声明标准的 `nvidia.com/gpu` 资源；创建 k3d 集群时选择宿主机 GPU。


## 5. 清理

删除集群：

```bash
k3d cluster delete "$CLUSTER"
```

删除集群会停止其中所有 Pod、删除集群内的 Kubernetes 资源，并释放分配给 k3d 节点容器的 GPU。
