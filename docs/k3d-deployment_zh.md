<!-- SPDX-License-Identifier: Apache-2.0 -->
<!-- SPDX-FileCopyrightText: Copyright contributors to the Foretoken project -->

# 使用 k3d 部署 Foretoken

[English](k3d-deployment.md) | [中文](k3d-deployment_zh.md)

k3d 在 Docker 容器中运行轻量级 Kubernetes 发行版 k3s。它适合在一台共享 GPU 服务器上创建相互隔离、可随时删除的 Foretoken 集群，同时继续使用标准 Helm、CRD 和 Kubernetes API。k3d 集群的节点位于同一台 Docker 主机；跨物理机器部署使用 k3s 或 Kubernetes。

创建 k3d 集群后，使用 `foretoken install` 安装发布镜像，或使用 `foretoken install -e .` 安装当前源码。源码模式会复用仓库构建流程，并只把发生变化的本地镜像导入当前 k3d 集群。

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

以下命令均从已经检出的 Foretoken 仓库根目录运行。

使用 pip 安装 CLI：

```bash
pip install -e .
```

或使用 uv 创建并激活虚拟环境后安装：

```bash
uv venv
source .venv/bin/activate
uv pip install -e .
```

### 4.1 选择部署方式

- **使用发布镜像**：继续执行 [第 4.2 节：本地模式](#42-本地模式) 或 [第 4.3 节：网关模式](#43-网关模式)。
- **以本地模式部署源码**：执行下面的完整命令，然后直接进入[第 4.4 节：发送请求](#44-发送-openai-api-兼容请求)。

```bash
foretoken install -e .
foretoken deploy examples/quickstart --timeout 6m
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

安装网关模式并使用发布镜像部署快速开始示例。集群没有已就绪的 Envoy Controller 时，CLI 会自动安装 Envoy Gateway：

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
