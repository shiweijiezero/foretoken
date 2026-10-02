<!-- SPDX-License-Identifier: Apache-2.0 -->
<!-- SPDX-FileCopyrightText: Copyright contributors to the Foretoken project -->

# 从源码部署 Foretoken

[English](custom-deployment.md) | [中文](custom-deployment_zh.md)

从本地源码构建 Foretoken，并将源码改动部署到 Kubernetes 集群。

## 从源码安装

本机需要 Python 3.11+、Git、kubectl 和 Helm。集群需要允许运行 BuildKit Pod，并有默认 StorageClass 保存持久编译缓存。需要其他存储类时，在 `deploy/platform-values.yaml` 中设置 `development.build.storageClassName`，并通过 `--values` 传入。

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

编译在专用 Pod 中执行，镜像直接载入集群节点。k3d 的 GPU 配置见[使用 k3d 部署 Foretoken](k3d-deployment_zh.md)。

### 远程集群与私有镜像仓库

其他集群需要构建 Pod 和目标节点均可访问的镜像仓库。将 `example` 替换为有推送权限的命名空间，并通过 Docker CLI 登录，为构建提供推送凭据：

```bash
export REGISTRY=ghcr.io/example/foretoken
docker login ghcr.io
```

使用私有仓库时，安装前先在 `foretoken-platform` 中创建名为 `registry-auth` 的镜像拉取 Secret；部署模型前，在各工作负载命名空间中创建同名 Secret。将引用保存到 `deploy/platform-values.yaml`：

```yaml
imagePullSecrets:
  - name: registry-auth
workload:
  imagePullSecrets:
    - name: registry-auth
```

带上该文件安装：

```bash
foretoken install -e . --registry "$REGISTRY" --values deploy/platform-values.yaml
```

镜像允许公开拉取且没有其他自定义设置时，省略 `--values`。仓库登录用于授权推送；镜像拉取 Secret 用于授权集群节点下载私有镜像。

## 部署与更新代码

在支持 GPU 的集群上部署仓库维护的[快速开始示例](../README_zh.md#快速开始)：

```bash
foretoken deploy examples/quickstart --timeout 20m
```

修改源码后，再执行同一条命令。命令沿用保存的安装设置，只发送新增、修改的文件和删除信息。专用构建 Pod 负责编译 Rust 改动、准备 Python 更新，编译缓存与产物留在集群。有可写的持久运行时存储时，这些更新无需重建运行时镜像；依赖、构建配置、控制面或启动引导代码变化时，自动走镜像构建流程。

受影响的工作负载会重启，并可能重新加载模型权重。命令等待所选代码和服务路由生效后退出。源码和部署配置均未变化时，现有工作负载保持运行。请求与清理操作沿用快速开始中的[发送请求](../README_zh.md#4-发送测试请求)和[停止与卸载](../README_zh.md#停止与卸载)。

CLI 的 Python 代码直接从 editable 源码目录加载；修改其 Python 依赖后，重新执行 `pip install -e .`。
## 修改推理引擎

修改 vLLM 时，关联与运行时 Python、PyTorch 和设备环境匹配的 Git 源码目录。假设源码位于 `../vllm`：

```bash
foretoken install -e . --engine-source ../vllm
```

原安装使用了 `--registry` 或 `--values` 时，保留这些选项。之后修改引擎源码，继续使用上面的 `foretoken deploy`。Python 和 Triton 改动同步源码，Triton JIT 编译由推理引擎执行；NVIDIA CUDA/C++ 改动在构建 Pod 中复用缓存编译 vLLM 扩展。

沐曦原生 kernel 由插件源码提供。将插件与匹配的 core 源码一起关联：

```bash
foretoken install -e . \
  --engine-source ../vllm \
  --engine-source vllm-metax=../vllm-metax
```

构建 Pod 为沐曦编译插件扩展；该后端不使用 core 中的 CUDA kernel。安装时保留原镜像仓库和 values 选项。

### 更换运行环境

运行时镜像提供 Python、PyTorch 和设备库。需要更换时，在 `deploy/platform-values.yaml` 中指定兼容镜像，将示例地址替换为集群构建器能够使用的镜像：

```yaml
runtime:
  vllm:
    image: ghcr.io/example/custom-vllm:latest
```

重新执行安装命令，传入 `--values deploy/platform-values.yaml`，并保留镜像仓库和引擎源码选项。使用 `-e` 时，Foretoken 以此镜像为构建基础，加入 model-server；安装完成后重新部署工作负载。

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

远程集群沿用[前文](#远程集群与私有镜像仓库)的仓库登录和 `REGISTRY` 设置，推送模型服务镜像：

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

远程平台构建保留 `--registry "$REGISTRY"`，以及原有的其他安装选项。修改 Omni 代码后重新构建并分发该镜像；前面的 vLLM editable 源码更新针对标准 vLLM 后端。
