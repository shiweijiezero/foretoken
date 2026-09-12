<!--
SPDX-License-Identifier: Apache-2.0
SPDX-FileCopyrightText: Copyright contributors to the Foretoken project
-->

# 准备沐曦 Foretoken 平台

[English](metax-platform.md) | 简体中文

本指南供集群管理员一次性准备沐曦镜像和 Foretoken 平台。完成后，模型用户只需按[部署与调用指南](../metax-deployment_zh.md)操作，无需理解底层推理引擎的安装过程。

Foretoken 使用三个镜像：controller 管理 Kubernetes 中的模型服务，frontend 接收请求，model-server 在沐曦 GPU 上执行模型。下面从同一份源码构建三个镜像，并安装配套的 Helm Chart，保证接口和 CRD（Kubernetes 自定义资源定义）与示例一致。

## 环境要求

集群管理员负责准备：

- Kubernetes 1.29 或更高版本、沐曦驱动和 MetaX device plugin；节点应发布 `metax-tech.com/gpu` 资源。
- 目标节点上的可写模型目录，或用于模型缓存的 StorageClass。按[模型存储](../model-storage_zh.md)配置示例的 `cache.yaml`。
- 可供客户端访问的 Gateway 地址；下面使用 Envoy Gateway。已有平台应由原管理员维护，不要安装第二套控制器接管它。

构建机器需要 Foretoken 源码、支持 BuildKit 的 Docker 和 Make；安装平台需要 kubectl、Helm 及对应集群权限。源码构建会访问 GitHub、PyPI、MetaX Python 软件源及容器镜像仓库。

使用监控时，先准备 Prometheus、Prometheus Operator、`ServiceMonitor`/`PrometheusRule` CRD 和覆盖沐曦节点的 mxExporter。Prometheus 需要选择平台及工作负载 namespace 中的监控资源；额外标签通过 `observability.additionalLabels` 配置。源码 Chart 不安装这些共享依赖，具体接入方式见[可观测性指南](../../observability/README_zh.md)。

## 构建镜像

所有命令从 Foretoken 仓库根目录执行。

### 1. 构建沐曦 model-server

默认从 MACA SDK 镜像构建。MACA 是沐曦 GPU 的工具链和运行库；基础镜像不需要预装 PyTorch 或 vLLM。准备安装了匹配 SDK 的 Ubuntu 24.04 镜像，或系统 Python 和开发头文件均为 3.12 的 Debian 系镜像，并用它替换 `<maca-sdk-image>`：

```bash
METAX_SDK_IMAGE=<maca-sdk-image> \
VLLM_METAX_VERSION=0.24.0 \
make image-model-server-metax
```

该命令在镜像内创建独立 uv 环境，安装公开源码及依赖，生成 `foretoken-vllm-metax:0.24.0` 和 `foretoken-model-server:dev`。Pod 使用镜像内的 Python，不读取宿主机虚拟环境。SDK 和驱动按[沐曦官方版本矩阵](https://vllm-metax.readthedocs.io/en/latest/getting_started/quickstart.html)匹配；0.24 发布线对应 MACA 3.8.2.x。

已有兼容 MetaX vLLM 镜像时，可用以下命令替代上面的构建。将镜像名称和 Python 路径替换为实际值：

```bash
INFERENCE_ENGINE_IMAGE=<metax-vllm-image> \
FORETOKEN_VLLM_PYTHON=/opt/conda/bin/python \
make image-model-server
```

两种方式都生成 `foretoken-model-server:dev`；接下来执行相同的平台镜像构建与安装步骤。

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

控制面启动前会初始化配套 CRD；Chart 创建供模型服务共用的 Gateway。Gateway 应获得客户端可达的地址并报告 `Programmed=True`。然后为用户提供集群访问配置、可用 namespace、服务域名和匹配的示例源码，转到[模型部署与调用](../metax-deployment_zh.md#1-部署示例模型)。

此路径通过 Helm 管理平台，不使用 `foretoken install -e` 重新构建或选择镜像。若不需要 Gateway，可改用 `frontend.mode: local` 和 `frontend.gateway.create: false`，并确保集群可为前端分配可访问的 LoadBalancer 地址。

## 卸载与排障

先由用户删除自己的模型部署，再由平台负责人执行：

```bash
helm uninstall foretoken --namespace foretoken-platform
```

CRD 会保留；缓存 PVC 按 RuntimeCache 保留策略清理。Envoy Gateway、监控和镜像由各自负责人管理。

- **镜像构建失败：** 查看下载、编译或依赖求解的原始错误，核对 SDK 版本和软件源可达性。
- **Pod 无法导入 Python 包：** 确认其实际镜像及 `FORETOKEN_VLLM_PYTHON`，不要将宿主机路径用于 Pod。
- **模型或缓存未就绪：** 查看 Pod 事件和 RuntimeCache 状态，检查 GPU 配额、存储绑定及在线扩容支持。
- **监控无数据：** 检查 Prometheus 选择器、namespace 范围、标签和 mxExporter 覆盖，见可观测性指南。

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

这是镜像构建使用的同一安装器，不继承系统 Python 包，也不跳过依赖求解。源码保留在安装目录的 `third_party` 中。失败目录保留供排查；解决原因后，用新的安装目录重试。激活脚本同时设置 MACA 编译器和库路径，运行时应先加载它。

当前独立安装验证组合是 vLLM/vLLM-metax 0.24.0、MetaX PyTorch 2.10 和 mcoplib 0.4.9。安装器回移[上游 XGrammar 依赖修正](https://github.com/MetaX-MACA/vLLM-metax/commit/1331d8ad37da9a69fe1140b7759633d509b722a9)，以 `+foretoken.1` 标识插件，并使用 Transformers 5.5.3、XGrammar 0.2.1 和 TVM FFI 0.1.9 的兼容组合。已验证文本与 JSON 约束输出；torchaudio 与 PyTorch 存在二进制兼容问题，不用于音频推理。Foretoken 的协议适配范围为 vLLM 0.20–0.28，这不等于每个版本都已完成独立安装或 GPU 验证。
