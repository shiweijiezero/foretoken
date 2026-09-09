<!--
SPDX-License-Identifier: Apache-2.0
SPDX-FileCopyrightText: Copyright contributors to the Foretoken project
-->

# 在沐曦 GPU 上部署 Foretoken

[English](metax-deployment.md) | 简体中文

Foretoken 通过 [`vLLM-metax`](https://github.com/MetaX-MACA/vLLM-metax) 硬件插件在沐曦 C 系列 GPU 上运行 vLLM。本指南从构建适配沐曦的 Foretoken model-server image 开始，完成 Kubernetes GPU 配置、单模型示例部署，并通过 Kubernetes Gateway 发送推理请求。

## 准备环境

开始前需要：

- Linux Kubernetes 1.29 或更高版本，并配置支持卷扩容的默认 `StorageClass`；
- 安装了 MetaX device plugin 的沐曦 C 系列 GPU 节点，节点能够发布 `metax-tech.com/gpu`；
- 覆盖所有沐曦 GPU 节点的官方 mxExporter 和兼容 `ServiceMonitor`；
- 支持 BuildKit 的 `docker`、`kubectl`、Helm 和 Foretoken CLI；
- 与目标版本对应的 MetaX 官方 vLLM image；
- GPU 节点可访问的 OCI registry，或向节点容器运行时导入镜像的权限。

维护中的单模型示例会部署两个 frontend 副本和一个模型副本，合计申请 1 张 GPU、8 个 CPU 和 52 GiB 内存，部署前应确认集群具备足够容量。

MetaX 将 vLLM、MACA、PyTorch 和 mcoplib 按经过测试的组合发布。基础镜像应从 [vLLM-MetaX 版本矩阵](https://vllm-metax.readthedocs.io/en/latest/getting_started/quickstart.html)选择，不要混用不同版本行中的软件包。Foretoken 安装时只发现和复用集群已有的 mxExporter，不负责安装它；发现规则见[可观测性指南](../observability/README_zh.md)。

## 构建 model-server image

以下命令都在 Foretoken 仓库根目录执行。可以直接复用官方运行时，也可以为源码开发创建 uv 覆盖环境。

### 复用 MetaX 官方 vLLM image

将 `INFERENCE_ENGINE_IMAGE` 设置为版本矩阵中选定的镜像。MACA、PyTorch、mcoplib、原生库和 vLLM 继续由该镜像提供，Foretoken 只加入 model-server 进程：

```bash
INFERENCE_ENGINE_IMAGE=<matching-metax-vllm-image> \
FORETOKEN_VLLM_PYTHON=/opt/conda/bin/python \
make image-model-server
```

构建结果为 `foretoken-model-server:dev`。

如果目标镜像通过其他 Python 解释器提供 vLLM，将 `FORETOKEN_VLLM_PYTHON` 改为镜像内对应的绝对路径。

### 使用 uv 管理公开源码覆盖层

进行源码开发时，Foretoken 可以在镜像内创建虚拟环境，并安装同版本的公开 `vLLM-metax` 与 upstream vLLM tag。构建机器需要能够访问 GitHub、PyPI 和 MetaX Python 软件源：

```bash
METAX_BASE_IMAGE=<matching-metax-vllm-image> \
VLLM_METAX_VERSION=0.24.0 \
make image-model-server-metax
```

该命令生成：

```text
foretoken-vllm-metax:0.24.0
foretoken-model-server:dev
```

基础镜像仍然负责经过验证的 MACA、PyTorch、mcoplib 和原生 ABI。`/opt/foretoken-vllm` 中的 uv 环境只覆盖安装选定的两个公开 `vX.Y.Z` tag。一个 `VLLM_METAX_VERSION` 同时选择两个仓库，避免版本漂移。

MetaX 公开源码构建入口当前支持 vLLM-MetaX 0.20 至 0.24。Foretoken EngineCore adapter 支持 vLLM 0.20 至 0.28；更高版本需要先完成明确的兼容更新。

## 将镜像提供给集群

集群能够访问 OCI registry 时，使用任务专属 tag 推送镜像：

```bash
export REGISTRY=<registry>/<project>
export MODEL_SERVER_IMAGE="$REGISTRY/foretoken-model-server:metax-v0.24.0"

docker tag foretoken-model-server:dev "$MODEL_SERVER_IMAGE"
docker push "$MODEL_SERVER_IMAGE"
```

离线集群可以先导出镜像，再由节点管理员导入每个可能承载模型工作负载的 GPU 节点：

```bash
docker save foretoken-model-server:dev \
  --output foretoken-model-server-metax.tar

sudo ctr --namespace k8s.io images import \
  foretoken-model-server-metax.tar
```

不要覆盖其他部署正在使用的镜像 tag。

## 配置并安装 Foretoken

创建 `metax-values.yaml`：

```yaml
runtime:
  vllm:
    image: <registry>/<project>/foretoken-model-server:metax-v0.24.0
    gpu:
      resourceName: metax-tech.com/gpu
      runtimeClassName: ""
```

私有 registry 还需要在每个工作负载 namespace 中创建 image pull Secret，并将名称写入 `workload.imagePullSecrets`。

先安装 Envoy Gateway，再以 Gateway 模式安装 Foretoken：

```bash
helm upgrade --install envoy-gateway \
  oci://docker.io/envoyproxy/gateway-helm \
  --namespace envoy-gateway-system \
  --create-namespace \
  --wait

foretoken install \
  --frontend-mode gateway \
  --values metax-values.yaml
```

如果平台由管理员统一维护，应直接复用，不要重复安装。此时需要确认平台使用了适配沐曦的 vLLM runtime image 和 GPU 资源名。

## 部署模型

在 `examples/quickstart/frontend.yaml` 中增加访问域名：

```yaml
spec:
  hostname: foretoken.example.com
```

部署维护中的单模型示例并等待服务就绪：

```bash
foretoken deploy examples/quickstart
foretoken status examples/quickstart

kubectl get frontendservice,modelservice,modelpool,modelgroup \
  --namespace foretoken-demo
kubectl get pods --namespace foretoken-demo --output wide
```

示例通过 `resources.requests.gpu.count` 申请一张加速卡，控制面会把它转换为平台配置的 `metax-tech.com/gpu` 资源请求。普通工作负载不需要 `privileged`，也不应挂载完整的宿主机 `/dev`；设备注入由 MetaX device plugin 负责。

## 发送请求

获取 Gateway 地址和请求所需的 HTTP Host：

```bash
export FORETOKEN_FRONTEND_URL="$(foretoken endpoint examples/quickstart)"
export FORETOKEN_REQUEST_HOST="$(foretoken endpoint examples/quickstart --host)"
```

先查看前端发布的模型标识：

```bash
curl --fail-with-body \
  "$FORETOKEN_FRONTEND_URL/v1/models" \
  -H "Host: $FORETOKEN_REQUEST_HOST"
```

使用返回的模型 ID 发送流式请求：

```bash
curl --fail-with-body --no-buffer \
  "$FORETOKEN_FRONTEND_URL/v1/chat/completions" \
  -H "Host: $FORETOKEN_REQUEST_HOST" \
  -H "Content-Type: application/json" \
  -d '{
    "model": "Qwen/Qwen3-0.6B",
    "messages": [{"role": "user", "content": "你好"}],
    "stream": true
  }'
```

正常响应会持续返回 `data:` 事件，并以 `data: [DONE]` 结束。

## 使用容器化 k3d 验证

特权 k3d 节点可以运行 MetaX device plugin，并挂载所需设备和运行库；但这些挂载本身不能隔离某一张物理 GPU。Docker 的 NVIDIA `--gpus` 参数不是 MetaX 的设备选择接口。在配置 MetaX 官方设备选择机制或容器 runtime 前，`metax-tech.com/gpu` 只能用于已验证的集群资源申请，不能将申请一张 GPU 解释为指定某一张宿主机 GPU。

## 清理

删除示例部署：

```bash
foretoken delete examples/quickstart
```

只有平台安装负责人才能执行 `foretoken uninstall`。不要用它删除共享平台或其他团队管理的 Gateway。

## 常见问题

- **Pod 无法导入 `torch` 或 `vllm`：** 检查 `FORETOKEN_VLLM_PYTHON` 是否指向目标镜像内的 Python。官方镜像通常使用 `/opt/conda/bin/python`，uv 覆盖环境会自动使用 `/opt/foretoken-vllm/bin/python`。
- **Pod 一直处于 Pending：** 使用 `kubectl describe pod` 检查目标节点是否还有可分配的 `metax-tech.com/gpu`。
- **Engine 启动时拒绝版本：** 确认 vLLM 和 `vLLM-metax` 使用相同 release，并处于 Foretoken 支持范围内。
- **Gateway 返回 404：** 检查 `HTTPRoute` 是否 Accepted，并确保请求携带 `foretoken endpoint --host` 返回的 Host。
- **接口返回 `model_not_found`：** 使用 `/v1/models` 返回的完整模型标识。
- **Frontend 返回 503：** 先确认 `ModelGroup` 和 model-server Pod 已经 Ready，再检查 Gateway 配置。
