<!-- SPDX-License-Identifier: Apache-2.0 -->
<!-- SPDX-FileCopyrightText: Copyright contributors to the Foretoken project -->

# 使用 k3d 部署 Foretoken

[English](k3d-deployment.md) | [中文](k3d-deployment_zh.md)

k3d 在 Docker container 中运行轻量 Kubernetes 发行版 k3s。它适合在一台共享 GPU 服务器上创建独立、可删除的 Foretoken cluster，同时继续使用标准 Helm、CRD 和 Kubernetes API。k3d cluster 的节点位于同一台 Docker 主机；跨物理机器部署使用 k3s 或 Kubernetes。

创建 k3d cluster 后，在仓库根目录运行 `make dev-deploy`。脚本会构建源码并将发生变化的本地镜像导入当前 cluster；后续 Helm 部署、Ready 等待和请求验证与其他 Kubernetes 集群相同。

## k3d 如何选择并登记 GPU

标准 Kubernetes Pod 请求的是 GPU 类型和数量：

```yaml
resources:
  limits:
    nvidia.com/gpu: 1
```

Pod 不指定宿主机 index。在这套开发配置中，Docker `--gpus` 与一致的 device-plugin 配置用来选择目标宿主机设备，并向 Kubernetes 登记它们的数量：

```text
宿主机物理 GPU 6、7
→ Docker --gpus '"device=6,7"'
→ k3d node container
→ k3s NVIDIA device plugin
→ Foretoken Pod
```

这是调度与设备选择机制，不是多租户安全隔离边界。k3d server 是 privileged Docker container；受宿主机 NVIDIA runtime 行为影响，node 内进程仍可能枚举到选定集合之外的设备。Node 报告 `nvidia.com/gpu: 2` 只表示 Kubernetes 可以调度两份标准 GPU 资源，不表示这个嵌套 node 在物理上无法访问其他宿主机 GPU。

在共享服务器上只应运行可信的开发 workload。推理 Pod 必须请求 `nvidia.com/gpu`，并由 runtime 和 device plugin 负责设备选择；不要把 `NVIDIA_VISIBLE_DEVICES` 当作权限控制。不可信租户需要硬件隔离时，请使用独立宿主机、VM 或按常规方式隔离的 Kubernetes node。

## 前置条件

主机需要：

- Linux；
- NVIDIA driver；
- NVIDIA Container Toolkit；
- 可使用 NVIDIA runtime 的 Docker；
- k3d、kubectl 和 Helm。

## 1. 选择 GPU 并命名 cluster

查看 GPU：

```bash
nvidia-smi
```

以下示例选择 GPU 6、7，并将 cluster 命名为 `foretoken-qwen-test`：

```bash
export GPU_INDICES=6,7
export CLUSTER=foretoken-qwen-test
```

## 2. 创建选定 GPU 的 k3d cluster

下面的 Bash 代码读取 NVIDIA runtime、配置和依赖库所在位置，并为 k3d 生成 mount 参数：

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

创建单 server cluster：

```bash
if k3d cluster get "$CLUSTER" >/dev/null 2>&1; then
  k3d cluster delete "$CLUSTER"
fi

k3d cluster create "$CLUSTER" \
  --config deploy/k3d/config.yaml \
  --gpus "\"device=$GPU_INDICES\"" \
  "${K3D_VOLUME_ARGS[@]}"
```

查看创建后的 node：

```bash
kubectl get nodes
```

## 3. 安装 NVIDIA device plugin

```bash
kubectl apply -f \
  https://raw.githubusercontent.com/NVIDIA/k8s-device-plugin/v0.17.4/deployments/static/nvidia-device-plugin.yml
```

内层 NVIDIA runtime 使用与外层 k3d 相同的宿主机 GPU 列表：

```bash
kubectl set env daemonset/nvidia-device-plugin-daemonset \
  --namespace kube-system \
  NVIDIA_VISIBLE_DEVICES="$GPU_INDICES"

kubectl rollout status daemonset/nvidia-device-plugin-daemonset \
  --namespace kube-system \
  --timeout=3m
```

显式验证 Kubernetes 可调度数量：

```bash
kubectl get nodes \
  --output jsonpath='{range .items[*]}{.metadata.name}{" capacity="}{.status.capacity.nvidia\.com/gpu}{" allocatable="}{.status.allocatable.nvidia\.com/gpu}{"\n"}{end}'
```

对于 `GPU_INDICES=6,7`，两个值都应为 `2`。这只验证 device plugin 的资源登记，不改变上面的隔离警告。

## 4. 安装并访问 Foretoken

进入 Foretoken 项目路径：

```bash
cd /path/to/your/foretoken
```

### 4.1 选择部署方式

- **使用发布镜像**：继续执行 [第 4.2 节：本地模式](#42-本地模式) 或 [第 4.3 节：网关模式](#43-网关模式)。
- **从源码部署**：依次完成 [第 2.1 节：直接导入本地镜像](custom-deployment_zh.md#21-直接导入本地镜像)、[第 4 节：部署 Quick Start](custom-deployment_zh.md#4-部署-quick-start可选) 和 [第 5 节：发送请求](custom-deployment_zh.md#5-发送请求可选)。

### 4.2 本地模式

使用发布镜像安装 Foretoken 并部署 Quick Start：

```bash
helm upgrade --install foretoken \
  oci://ghcr.io/shiweijiezero/foretoken/charts/foretoken \
  --namespace foretoken-platform \
  --create-namespace \
  --set frontend.enabled=true \
  --set frontend.mode=local \
  --wait \
  --debug

kubectl apply --server-side -k examples/quickstart

kubectl wait --for=condition=Ready \
  --namespace foretoken-demo \
  --timeout=15m \
  frontendservice/quickstart-frontend \
  modelservice/quickstart-qwen3-0.6b
```

读取 k3s ServiceLB 为 frontend 分配的地址：

```bash
export FRONTEND_HOST="$(kubectl get service quickstart-frontend \
  --namespace foretoken-demo \
  -o jsonpath='{.status.loadBalancer.ingress[0].ip}{.status.loadBalancer.ingress[0].hostname}')"
export FRONTEND_URL="http://$FRONTEND_HOST:8080"
```

### 4.3 网关模式

先在 `examples/quickstart/frontend.yaml` 中设置对外域名：

```yaml
spec:
  hostname: foretoken.example.com
```

安装 Envoy Gateway，并使用发布镜像部署 Foretoken 和 Quick Start：

```bash
helm upgrade --install envoy-gateway \
  oci://docker.io/envoyproxy/gateway-helm \
  --namespace envoy-gateway-system \
  --create-namespace \
  --wait \
  --debug

helm upgrade --install foretoken \
  oci://ghcr.io/shiweijiezero/foretoken/charts/foretoken \
  --namespace foretoken-platform \
  --create-namespace \
  --set frontend.enabled=true \
  --set frontend.mode=gateway \
  --set frontend.gateway.create=true \
  --wait \
  --debug

kubectl apply --server-side -k examples/quickstart

kubectl wait --for=condition=Ready \
  --namespace foretoken-demo \
  --timeout=15m \
  frontendservice/quickstart-frontend \
  modelservice/quickstart-qwen3-0.6b
```

使用已配置的域名：

```bash
export FRONTEND_URL=https://foretoken.example.com
```

### 4.4 发送 OpenAI API 兼容格式的请求

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

Foretoken YAML 继续请求标准 `nvidia.com/gpu` 资源。k3d 与 device-plugin 配置为调度选择目标宿主机 GPU 集合；需要确认准确物理映射时，请用 UUID 与 `nvidia-smi` 对照。

## 5. 继续配置可观测性

验证推理服务后，可以按照[加速器可观测性](accelerator-observability_zh.md)选择 [NVIDIA DCGM 适配器](accelerators/nvidia-dcgm_zh.md)。它的 k3d overlay 会为 exporter 显式选择同一组可信 GPU，但不占用 Kubernetes GPU 配额。

## 6. 清理

删除 cluster：

```bash
k3d cluster delete "$CLUSTER"
```

删除 cluster 会停止其中所有 Pod、删除 cluster 内 Kubernetes 资源，并释放传给 k3d node container 的 GPU。
