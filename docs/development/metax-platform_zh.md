<!--
SPDX-License-Identifier: Apache-2.0
SPDX-FileCopyrightText: Copyright contributors to the Foretoken project
-->

# 准备沐曦 Foretoken 平台

[English](metax-platform.md) | 简体中文

在沐曦 GPU 集群上安装 Foretoken，或构建自定义运行时镜像。模型部署见[部署与调用指南](../metax-deployment_zh.md)。

## 环境要求

集群管理员负责准备：

- Kubernetes 1.29 或更高版本、沐曦驱动和 MetaX device plugin；节点应发布 `metax-tech.com/gpu` 资源。
- 目标节点上的可写模型目录，或用于模型缓存的 StorageClass。按[模型存储](../model-storage_zh.md)配置示例的 `cache.yaml`。
- 可供客户端访问的 LoadBalancer 或 Gateway 地址。

构建机器需要 Foretoken 源码、支持 BuildKit 的 Docker 和 Make；安装平台需要 kubectl、Helm 及对应集群权限。源码构建会访问 GitHub、PyPI、MetaX Python 软件源及容器镜像仓库。

## 安装发布版

```bash
foretoken install
```

自动选择沐曦镜像；Gateway 和自定义配置见 [CLI 安装指南](../../cli/README_zh.md#安装-kubernetes-平台)。

## 构建镜像

需要自定义 SDK 或推理运行时时，按以下步骤构建。所有命令从 Foretoken 仓库根目录执行。

### 1. 构建沐曦 model-server

默认从 MACA SDK 镜像构建。MACA 是沐曦 GPU 的工具链和运行库；基础镜像不需要预装 PyTorch 或 vLLM。准备安装了匹配 SDK 的 Ubuntu 24.04 镜像，或系统 Python 和开发头文件均为 3.12 的 Debian 系镜像，并用它替换 `<maca-sdk-image>`：

```bash
METAX_SDK_IMAGE=<maca-sdk-image> \
VLLM_METAX_VERSION=0.24.0 \
make image-model-server-metax
```

生成 `foretoken-vllm-metax:0.24.0` 和 `foretoken-model-server:dev`。SDK 和驱动按[沐曦官方版本矩阵](https://vllm-metax.readthedocs.io/en/latest/getting_started/quickstart.html)匹配。

已有兼容 MetaX vLLM 镜像时，可用以下命令替代上面的构建。将镜像名称和 Python 路径替换为实际值：

```bash
INFERENCE_ENGINE_IMAGE=<metax-vllm-image> \
FORETOKEN_VLLM_PYTHON=/opt/conda/bin/python \
make image-model-server
```

### 2. 构建 controller 和 frontend

```bash
make image-frontend
docker build -f control-plane/Dockerfile -t foretoken-control-plane:dev .
```

### 3. 将镜像提供给节点

将 `<registry>/<project>` 替换为节点可访问、当前账号可推送的仓库前缀：

```bash
export REGISTRY=<registry>/<project>
export MODEL_SERVER_IMAGE="$REGISTRY/foretoken-model-server:metax-v0.24.0"
export FRONTEND_IMAGE="$REGISTRY/foretoken-frontend:metax-v0.24.0"
export CONTROL_PLANE_IMAGE="$REGISTRY/foretoken-control-plane:metax-v0.24.0"

docker tag foretoken-model-server:dev "$MODEL_SERVER_IMAGE"
docker tag foretoken-frontend:dev "$FRONTEND_IMAGE"
docker tag foretoken-control-plane:dev "$CONTROL_PLANE_IMAGE"
docker push "$MODEL_SERVER_IMAGE"
docker push "$FRONTEND_IMAGE"
docker push "$CONTROL_PLANE_IMAGE"
```

离线集群由节点管理员[直接导入这三个镜像](source-image-lifecycle_zh.md#直接导入本地镜像)，后续配置使用实际导入的名称和 tag。

## 安装平台

手动使用 Helm 安装时，需按[可观测性指南](../../observability/README_zh.md)另行准备监控。

创建 `metax-values.yaml`，用实际发布或导入的镜像名称替换示例值：

```yaml
image:
  repository: <registry>/<project>/foretoken-control-plane
  tag: metax-v0.24.0
frontend:
  enabled: true
  mode: gateway
  gateway:
    create: true
  image: <registry>/<project>/foretoken-frontend:metax-v0.24.0
runtime:
  vllm:
    image: <registry>/<project>/foretoken-model-server:metax-v0.24.0
    gpu:
      resourceName: metax-tech.com/gpu
      runtimeClassName: ""
```

私有仓库的拉取凭据分别通过 `imagePullSecrets`（控制面）和 `workload.imagePullSecrets`（模型与前端）配置；对应 Secret 必须存在于各自 namespace，详见[私有仓库配置](../custom-deployment_zh.md)。

集群尚未安装 Envoy Gateway 时，由管理员安装一次：

```bash
helm upgrade --install envoy-gateway \
  oci://docker.io/envoyproxy/gateway-helm \
  --namespace envoy-gateway-system \
  --create-namespace \
  --wait
```

再安装构建镜像时使用的同一份源码 Chart：

```bash
helm upgrade --install foretoken ./deploy/charts/foretoken \
  --namespace foretoken-platform \
  --create-namespace \
  --values metax-values.yaml \
  --wait

kubectl get pods --namespace foretoken-platform
kubectl get gateway --namespace foretoken-platform
```

Gateway 应有可访问的地址，并报告 `Programmed=True`。然后按[模型部署与调用](../metax-deployment_zh.md#1-部署示例模型)部署模型。

若不需要 Gateway，可改用 `frontend.mode: local` 和 `frontend.gateway.create: false`，并确保集群可为前端分配可访问的 LoadBalancer 地址。

## 卸载

删除模型部署后，按安装方式卸载：

```bash
# CLI 安装
foretoken uninstall

# 手动 Helm 安装
helm uninstall foretoken --namespace foretoken-platform
```

CRD 和复用的集群资源会保留。

## 可选：在主机上开发推理引擎

仅调试底层 vLLM 或依赖时需要本节，普通 Foretoken 部署不需要。主机需匹配的 MACA SDK、Python 3.12 及开发头文件、C/C++ 工具、Bash、curl、tar、patch、uv，以及 libelf、libnuma、GLib、libpng、libjpeg 系统库。

安装目录必须尚不存在，从仓库根目录执行：

```bash
export MACA_PATH=/opt/maca
export UV_PYTHON=3.12
export VLLM_ENV="$PWD/.gpu_cache/metax-vllm-0.24.0"

bash deploy/inference-engines/vllm-metax/install.sh "$VLLM_ENV" 0.24.0
source "$VLLM_ENV/activate"
uv pip check --python "$VLLM_ENV/.venv/bin/python"
```

运行推理引擎前加载 `activate`，以设置 Python、MACA 编译器和库路径。
