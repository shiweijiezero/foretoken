<!--
SPDX-License-Identifier: Apache-2.0
SPDX-FileCopyrightText: Copyright contributors to the Foretoken project
-->

# 在沐曦 GPU 上部署 Foretoken

[English](metax-deployment.md) | 简体中文

Foretoken 通过 [`vLLM-metax`](https://github.com/MetaX-MACA/vLLM-metax) 硬件插件使用沐曦 GPU。可以在已有驱动和 MACA SDK 的机器上，从公开源码创建独立 uv 环境运行文本推理，也可以将同一环境构建为 Kubernetes 镜像；不需要预装完整 MetaX vLLM 镜像。

## 在独立 uv 环境中安装 vLLM

主机需要沐曦 C 系列 GPU、匹配的驱动与 MACA SDK、Python 3.12 及开发头文件、C/C++ 编译工具、Bash、curl、tar、patch 和 uv。MACA SDK 及其 libelf、libnuma、GLib、libpng、libjpeg 系统依赖由管理员安装；Python 包从 PyPI 和 [MetaX 软件源](https://repos.metax-tech.com/r/maca-pypi/simple/)获取。构建机器还需能够访问 GitHub。

以下示例使用 vLLM-metax 0.24.0、MetaX PyTorch 2.10 和 mcoplib 0.4.9。该发布线对应 MACA 3.8.2.x，驱动与 SDK 需符合[官方版本矩阵](https://vllm-metax.readthedocs.io/en/latest/getting_started/quickstart.html)。

从 Foretoken 仓库根目录执行，安装目录必须尚不存在：

```bash
export MACA_PATH=/opt/maca
export UV_PYTHON=3.12
export VLLM_ENV="$PWD/.gpu_cache/metax-vllm-0.24.0"

bash deploy/inference-engines/vllm-metax/install.sh "$VLLM_ENV" 0.24.0
source "$VLLM_ENV/activate"
```

安装器下载相同版本的 `vLLM-metax` 和 upstream vLLM tag，在 `$VLLM_ENV/.venv` 中安装全部 Python 依赖，并保留源码到 `$VLLM_ENV/third_party`。它不继承系统 site-packages，也不跳过依赖求解。MACA 原生内核由插件和 mcoplib 提供，upstream vLLM 使用 `VLLM_TARGET_DEVICE=empty` 构建，不安装 NVIDIA CUDA 版 vLLM wheel。

完整安装成功后，检查环境和 GPU：

```bash
uv pip check --python "$VLLM_ENV/.venv/bin/python"
python -c 'import torch, vllm; print(torch.__version__, vllm.__version__); print(torch.cuda.is_available())'
```

单机使用可以直接启动推理服务：

```bash
vllm serve Qwen/Qwen3-0.6B
```

宿主机独立环境用于本机运行。Kubernetes Pod 不会读取宿主机 venv，需按下一节构建镜像。

### 版本范围

独立源码安装的验证组合为 0.24.0。安装器为该版本回移 [MetaX 的 XGrammar 依赖修正](https://github.com/MetaX-MACA/vLLM-metax/commit/1331d8ad37da9a69fe1140b7759633d509b722a9)，插件安装版本含 `+foretoken.1`，与原始发布包区分。Transformers 5.5.3、XGrammar 0.2.1 和 TVM FFI 0.1.9 用于保持文本导入及 TileLang 原生接口兼容；仍执行完整依赖求解和 `uv pip check`。

该组合已验证文本和 JSON 约束输出，不用于音频推理：官方 torchaudio 2.4.1 wheel 与 PyTorch 2.10 存在加载时 ABI 冲突。EngineCore 协议适配接受 vLLM 0.20–0.28，但不等于这些版本都已通过独立安装或 GPU 验证。

## 构建 Kubernetes 镜像

### 从 MACA SDK 镜像构建

准备一个安装了匹配 MACA SDK 的 Ubuntu/Debian 镜像，不要求它含有 PyTorch、mcoplib 或 vLLM。构建需要支持 BuildKit 的 Docker：

```bash
METAX_SDK_IMAGE=<maca-sdk-image> \
VLLM_METAX_VERSION=0.24.0 \
make image-model-server-metax
```

镜像构建调用同一个 `install.sh`，生成 `foretoken-vllm-metax:0.24.0` 与 `foretoken-model-server:dev`。model-server 使用镜像内的 `/opt/foretoken-vllm/.venv/bin/python`，不挂载宿主机 venv。

### 可选：复用已准备好的 MetaX vLLM 镜像

已经有可用运行时镜像时，可以省去源码和 Python 依赖安装，只加入 Foretoken model-server：

```bash
INFERENCE_ENGINE_IMAGE=<metax-vllm-image> \
FORETOKEN_VLLM_PYTHON=/opt/conda/bin/python \
make image-model-server
```

将解释器路径改为目标镜像实际提供 vLLM 的 Python 路径。构建结果同样为 `foretoken-model-server:dev`。

## 将镜像提供给集群

将 `<registry>/<project>` 替换为 GPU 节点可访问的镜像仓库：

```bash
export MODEL_SERVER_IMAGE=<registry>/<project>/foretoken-model-server:metax-v0.24.0

docker tag foretoken-model-server:dev "$MODEL_SERVER_IMAGE"
docker push "$MODEL_SERVER_IMAGE"
```

离线集群可由节点管理员导入镜像，具体方式见[源码镜像生命周期](development/source-image-lifecycle_zh.md)。`runtime.vllm.image` 必须与实际导入的镜像名称和 tag 一致。

## 配置并安装 Foretoken

集群需要 Kubernetes 1.29 或更高版本、默认 `StorageClass`、发布 `metax-tech.com/gpu` 的 MetaX device plugin，以及覆盖沐曦节点的 mxExporter 和兼容 `ServiceMonitor`。Foretoken 只发现和复用已有 mxExporter，安装前请按[可观测性指南](../observability/README_zh.md)准备。工作站需要 Foretoken CLI、kubectl 和 Helm。

维护中的单模型示例部署两个 frontend 副本和一个模型副本，合计申请 1 张 GPU、8 个 CPU 和 52 GiB 内存。

创建 `metax-values.yaml`，把 image 改为上一步发布或导入的完整名称：

```yaml
runtime:
  vllm:
    image: <registry>/<project>/foretoken-model-server:metax-v0.24.0
    gpu:
      resourceName: metax-tech.com/gpu
      runtimeClassName: ""
```

私有仓库需要在工作负载 namespace 中创建 image pull Secret，并配置 `workload.imagePullSecrets`。

先安装 Envoy Gateway，再安装 Foretoken：

```bash
helm upgrade --install envoy-gateway \
  oci://docker.io/envoyproxy/gateway-helm \
  --namespace envoy-gateway-system \
  --create-namespace \
  --wait

foretoken install --frontend-mode gateway --values metax-values.yaml
```

共享平台由管理员统一配置时，直接复用，不重复安装。

## 部署模型并发送请求

在 `examples/quickstart/frontend.yaml` 已有的 `spec` 中增加 hostname：

```yaml
spec:
  hostname: foretoken.example.com
```

部署并查看状态：

```bash
foretoken deploy examples/quickstart
foretoken status examples/quickstart
kubectl get pods --namespace foretoken-demo --output wide
```

示例的 `resources.requests.gpu.count: 1` 会映射为 `metax-tech.com/gpu` 请求。设备由 MetaX device plugin 注入，无需给普通 Pod 添加 `privileged` 或挂载完整 `/dev`。

获取入口并查看模型标识：

```bash
FORETOKEN_FRONTEND_URL="$(foretoken endpoint examples/quickstart)"
FORETOKEN_REQUEST_HOST="$(foretoken endpoint examples/quickstart --host)"

curl --fail-with-body "$FORETOKEN_FRONTEND_URL/v1/models" \
  -H "Host: $FORETOKEN_REQUEST_HOST"
```

默认示例的模型 ID 为 `Qwen/Qwen3-0.6B`；修改过模型时，使用 `/v1/models` 返回的 ID：

```bash
curl --fail-with-body --no-buffer \
  "$FORETOKEN_FRONTEND_URL/v1/chat/completions" \
  -H "Host: $FORETOKEN_REQUEST_HOST" \
  -H "Content-Type: application/json" \
  -d '{"model":"Qwen/Qwen3-0.6B","messages":[{"role":"user","content":"你好"}],"stream":true}'
```

流式响应以 `data: [DONE]` 结束。

## 清理与排障

使用 `foretoken delete examples/quickstart` 删除示例。只有平台安装负责人才能执行 `foretoken uninstall`；本机 uv 环境和构建镜像由创建者管理。

- **安装依赖冲突：** 检查所选 release 与官方矩阵，保留 uv 的原始依赖错误，不跳过依赖或随意降级原生库。
- **无法加载 MACA 库：** 确认已激活安装目录中的 `activate`，SDK 与驱动兼容；容器需要由设备插件或运行时提供驱动与设备。
- **Pod 一直 Pending：** 用 `kubectl describe pod` 检查 GPU 和其他资源是否足够。
- **无法导入 `torch` 或 `vllm`：** 检查 `FORETOKEN_VLLM_PYTHON` 指向的环境，不要使用宿主机的解释器路径配置 Pod。
- **Gateway 404 或 `model_not_found`：** 分别检查请求 Host 和 `/v1/models` 返回的 ID。
- **Frontend 503：** 先检查 ModelGroup 和 model-server Pod 是否 Ready。
