<!-- SPDX-License-Identifier: Apache-2.0 -->
<!-- SPDX-FileCopyrightText: Copyright contributors to the Foretoken project -->

# 使用 k3d 部署 Foretoken

[English](k3d-deployment.md) | [中文](k3d-deployment_zh.md)

k3d 在 Docker 容器中运行轻量级 Kubernetes 发行版 k3s。它适合在一台共享 GPU 服务器上创建相互隔离、可随时删除的 Foretoken 集群，同时继续使用标准 Helm、CRD 和 Kubernetes API。k3d 集群的节点位于同一台 Docker 主机；跨物理机器部署使用 k3s 或 Kubernetes。

## 前置条件

主机需要：

- Python 3.11 或更高版本；
- Linux；
- NVIDIA 驱动程序；
- NVIDIA Container Toolkit；
- 可使用 NVIDIA 运行时的 Docker；
- k3d、kubectl 和 Helm。

## 1. 进入仓库并选择 GPU

获取源码后，从仓库根目录执行后续命令：

```bash
git clone https://github.com/shiweijiezero/foretoken.git
cd foretoken
```

查看 GPU：

```bash
nvidia-smi
```

选择没有其他任务的 GPU。快速开始需要 1 张 GPU、8 个 CPU 和 52 GiB 内存；还需为平台预留额外容量。下面以 GPU 6、7 和集群名 `foretoken-qwen-test` 为例，按实际空卡修改。Docker 限定节点可见的物理卡，Pod 再从中申请 GPU 数量：

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

# 模型下载和运行时缓存保存在示例目录中。
mkdir -p examples/quickstart/data
add_k3d_mount "$(realpath examples/quickstart/data)"
```

为 frontend 和 model-server 的运行用户配置 `examples/quickstart/data` 写权限；标准 frontend 使用 UID/GID 65532。其他存储方式见[模型存储](model-storage_zh.md)。

创建包含单个 server 节点的集群：

```bash
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

### 4.1 选择部署方式

下面的目录型示例使用当前仓库构建的平台。按[源码部署指南](custom-deployment_zh.md)准备工具，然后安装：

```bash
pip install -e .
foretoken install -e .
```

使用发布包和镜像时，从所选[发布页面](https://github.com/shiweijiezero/foretoken/releases)取得示例，再安装：

```bash
pip install foretoken
foretoken install
```

### 4.2 本地模式

部署快速开始示例，解析 k3s ServiceLB 为前端分配的地址：

```bash
foretoken deploy examples/quickstart --timeout 20m
FORETOKEN_FRONTEND_URL="$(foretoken endpoint examples/quickstart)"
FORETOKEN_REQUEST_HOST="$(foretoken endpoint examples/quickstart --host)"
```

### 4.3 网关模式

先在 `examples/quickstart/frontend.yaml` 中设置对外域名：

```yaml
spec:
  hostname: foretoken.example.com
```

启用网关模式并部署快速开始示例，命令会按需安装 Envoy Gateway：

```bash
foretoken install -e . --frontend-mode gateway
# 发布安装使用：foretoken install --frontend-mode gateway
foretoken deploy examples/quickstart --timeout 20m
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

## 5. 清理

删除集群：

```bash
k3d cluster delete "$CLUSTER"
```

删除集群会停止其中的 Pod 并释放 GPU。保留 `examples/quickstart/data`，创建新集群时恢复相同 bind mount，即可复用已下载的模型。
