<!-- SPDX-License-Identifier: Apache-2.0 -->
<!-- SPDX-FileCopyrightText: Copyright contributors to the Foretoken project -->

# 从源码部署 Foretoken

[English](custom-deployment.md) | [中文](custom-deployment_zh.md)

从本地源码构建 Foretoken，并将源码改动部署到 Kubernetes 集群。

## 从源码安装

本机需要 Python 3.11+、Git、kubectl 和 Helm。集群需要允许运行 BuildKit Pod 和发布 Job，并有默认 StorageClass 保存编译缓存与发布的应用文件。需要自定义存储类时，在 `deploy/platform-values.yaml` 中分别用 `development.build.storageClassName` 或 `applicationFiles.storageClassName` 覆盖，并通过 `--values` 传入。

```bash
git clone https://github.com/shiweijiezero/foretoken.git
cd foretoken
kubectl config current-context
```

从当前源码安装 CLI：

```bash
pip install -e .
```

本地 kind 或 k3d 集群无需镜像仓库，可直接构建并安装：

```bash
foretoken install -e .
```

编译在集群中执行。k3d 的 GPU 配置见[使用 k3d 部署 Foretoken](k3d-deployment_zh.md)。

## 部署与更新代码

在支持 GPU 的集群上部署仓库维护的[快速开始示例](../README_zh.md#快速开始)：

```bash
foretoken deploy examples/quickstart --timeout 20m
```

修改源码后，再执行同一条命令。代码更新沿用保存的安装设置和运行环境；运行环境依赖或镜像构建配置变化时，会更新平台安装。

受影响的工作负载会重启，并可能重新加载模型权重。命令等待所选代码和服务路由生效后退出。源码和部署配置均未变化时，现有工作负载保持运行。请求与清理操作沿用快速开始中的[发送请求](../README_zh.md#4-发送测试请求)和[停止与卸载](../README_zh.md#停止与卸载)。

CLI 的 Python 代码直接从 editable 源码目录加载；修改其 Python 依赖后，重新执行 `pip install -e .`。

## 修改推理引擎

修改 vLLM 时，关联与运行时 Python、PyTorch 和设备环境匹配的 Git 源码目录。假设源码位于 `../vllm`：

```bash
foretoken install -e . --engine-source ../vllm
```

原安装使用了 `--registry` 或 `--values` 时，保留这些选项。修改 Python、Triton 或 NVIDIA CUDA/C++ 源码后，继续使用上面的 `foretoken deploy`，所需编译会自动完成。

沐曦原生 kernel 由插件源码提供。将插件与匹配的 core 源码一起关联：

```bash
foretoken install -e . \
  --engine-source ../vllm \
  --engine-source vllm-metax=../vllm-metax
```

### 更换运行环境

运行时镜像提供 Python、PyTorch 和设备库。需要更换时，在 `deploy/platform-values.yaml` 中指定兼容镜像，将示例地址替换为集群构建器能够使用的镜像：

```yaml
runtime:
  vllm:
    image: ghcr.io/example/custom-vllm:latest
```

重新执行安装命令，传入 `--values deploy/platform-values.yaml`，并保留镜像仓库和引擎源码选项。使用 `-e` 时，Foretoken 以此镜像为构建基础；安装完成后重新部署工作负载。

沐曦基础镜像构建见[准备沐曦 Foretoken 平台](development/metax-platform_zh.md#从源码安装)。

### vLLM-Omni 运行时

vLLM-Omni 使用独立的模型服务镜像。在具备 Docker BuildKit、Make 和 rustup 管理的 Rust 工具链的机器上，从仓库根目录构建：

```bash
make image-vllm-omni VLLM_OMNI_IMAGE=foretoken-vllm-omni:latest
make image-model-server-omni \
  INFERENCE_ENGINE_IMAGE=foretoken-vllm-omni:latest \
  OMNI_MODEL_SERVER_IMAGE=foretoken-omni-model-server:latest
```

使用 k3d 时，将 `CLUSTER` 设为已有集群的名称，再导入镜像：

```bash
CLUSTER=foretoken-qwen-test
k3d image import --cluster "$CLUSTER" foretoken-omni-model-server:latest
```

远程集群沿用 [Kubernetes 部署指南](kubernetes-deployment_zh.md) 中的 `REGISTRY` 设置；仓库需要认证时，先完成登录，再推送模型服务镜像：

```bash
docker tag foretoken-omni-model-server:latest "$REGISTRY/omni-model-server:latest"
docker push "$REGISTRY/omni-model-server:latest"
```

在 `deploy/platform-values.yaml` 中将 `runtime.vllmOmni.image` 设为节点可拉取的镜像。以下使用本地镜像名；通过仓库分发时，改为推送后的完整地址：

```yaml
runtime:
  vllmOmni:
    image: foretoken-omni-model-server:latest
```

应用运行时设置：

```bash
foretoken install -e . --values deploy/platform-values.yaml
```

远程平台构建保留 `--registry "$REGISTRY"`，以及原有的其他安装选项。前面的 vLLM editable 源码更新针对标准 vLLM 后端。

安装完成后，用 `foretoken deploy` 部署 Omni 服务的 Kustomize 目录。修改代码后，重新构建并分发 Omni 镜像，将 `runtime.vllmOmni.image` 改为新的 tag 或 digest 引用，再执行安装命令并重新部署同一 Kustomize 目录。
