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

## 1. 准备 Linux GPU 主机

下面命令适用于 Ubuntu 或 Debian 系统。宿主机依赖只需安装一次；不要使用 `sudo` 运行 `foretoken`。

```bash
sudo apt-get update
sudo apt-get install -y docker.io curl ca-certificates gnupg
sudo usermod -aG docker "$USER"
newgrp docker
```

安装 NVIDIA Container Toolkit 并配置 Docker：

```bash
distribution=$(. /etc/os-release; echo "$ID$VERSION_ID")
curl -fsSL https://nvidia.github.io/libnvidia-container/gpgkey |
  sudo gpg --dearmor -o /usr/share/keyrings/nvidia-container-toolkit-keyring.gpg
curl -fsSL "https://nvidia.github.io/libnvidia-container/$distribution/libnvidia-container.list" |
  sed 's#^deb https://#deb [signed-by=/usr/share/keyrings/nvidia-container-toolkit-keyring.gpg] https://#' |
  sudo tee /etc/apt/sources.list.d/nvidia-container-toolkit.list >/dev/null
sudo apt-get update
sudo apt-get install -y nvidia-container-toolkit
sudo nvidia-ctk runtime configure --runtime=docker
```

安装 k3d、kubectl 和 Helm，然后检查主机：

```bash
curl -s https://raw.githubusercontent.com/k3d-io/k3d/main/install.sh | bash
curl -fsSL https://raw.githubusercontent.com/helm/helm/main/scripts/get-helm-3 | bash
curl -fsSL https://dl.k8s.io/release/stable.txt -o /tmp/kubectl-version
curl -fsSLO "https://dl.k8s.io/release/$(cat /tmp/kubectl-version)/bin/linux/amd64/kubectl"
sudo install -m 0755 kubectl /usr/local/bin/kubectl
rm kubectl /tmp/kubectl-version

nvidia-smi
docker info
k3d version
kubectl version --client
helm version --short
```

## 3. 进入仓库并选择 GPU

获取源码后，从仓库根目录执行后续命令：

```bash
git clone https://github.com/shiweijiezero/foretoken.git
cd foretoken
pip install -e .
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

## 4. 创建限定 GPU 的 k3d 集群

```bash
foretoken cluster create k3d --name "$CLUSTER" --gpus "$GPU_INDICES"
kubectl get nodes
```

命令会挂载 NVIDIA 运行时和仓库中的 `data/` 目录，安装 NVIDIA 设备插件，并切换到新建集群的 kubeconfig context。

## 5. 安装并访问 Foretoken

### 4.1 选择部署方式

从当前源码构建并安装集群平台：

```bash
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

启用网关模式并部署快速开始示例：

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
foretoken cluster delete k3d --name "$CLUSTER"
```

删除集群会停止其中的 Pod 并释放 GPU。保留 `data`，创建新集群时恢复相同 bind mount，即可复用已下载的模型。
