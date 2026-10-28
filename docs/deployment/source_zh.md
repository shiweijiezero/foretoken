<!-- SPDX-License-Identifier: Apache-2.0 -->
<!-- SPDX-FileCopyrightText: Copyright contributors to the Foretoken project -->

# 从源码部署 Foretoken

[English](source.md) | [中文](source_zh.md)

从本地源码目录构建 Foretoken，并将改动部署到 Kubernetes 集群。普通文件夹、clone 和 worktree 使用相同命令，构建依赖按源码中声明的版本自动准备。

优先复用已有兼容镜像和构建缓存，再在该环境中更新应用代码。日常源码改动走增量编译与发布；缺少环境，或依赖和构建输入变化时，才可能需要生成新镜像。

## 从源码安装

准备 kubectl 和 [Helm 4](../../cli/README_zh.md#开始前)，选择有 GPU 和默认存储类（StorageClass）的 Kubernetes 集群。共享集群的[源码构建权限](../../cli/README_zh.md#为成员准备命名空间)由管理员分配。

```bash
git clone https://github.com/shiweijiezero/foretoken.git
cd foretoken
kubectl config current-context

uv venv --python 3.12 --seed  # 使用 pip 时：python3 -m venv .venv（Python 3.11+）
source .venv/bin/activate
uv pip install -e .  # 或 pip install -e .
```

后续命令在已激活的环境中执行。

集群只需安装一次源码版平台。平台已经从源码安装时，可直接进入[部署步骤](#部署模型服务)；另一台工作站只需安装 CLI 并准备源码目录。已有发布版平台首次使用 `deploy -e` 前，仍需执行一次源码安装，这一步也会复用可用镜像和构建缓存。

首次在本地 kind 或 k3d 集群安装时，无需镜像仓库，可直接构建并安装：

```bash
foretoken install -e .
```

这会安装共享的 Foretoken 平台，构建在集群中进行；也可替换已通过 CLI 安装的发布版平台。k3d 的 GPU 配置见[使用 k3d 部署 Foretoken](k3d_zh.md)。其他 Kubernetes 集群需要构建任务和节点都能访问的镜像仓库，安装命令及仓库认证见[Kubernetes 部署指南](kubernetes_zh.md#2-从源码安装-foretoken)。

首次源码安装需要准备工具链和运行环境。后续部署会复用已有镜像、下载的依赖和兼容的编译缓存，因此代码更新通常更快。每个源码目录保留自己的增量构建状态。安装或部署失败时，修正问题后在原源码目录、原集群重跑同一命令，可继续复用已保留的构建缓存。

需要将构建产物存入其他存储类时，在 `deploy/platform-values.yaml` 中设置 `development.build.storageClassName`，通过 `--values` 传入；执行和编译缓存仍由框架自动安排在本地存储。支持动态供卷的原生 NFS 和 `nfs.csi.k8s.io`。应用文件使用集群默认存储类，也可通过 `applicationFiles.storageClassName` 指定。已有卷保留其存储类。

## 部署模型服务

在 Foretoken 仓库根目录部署快速开始示例，使用当前目录的代码。平台尚未准备默认模型存储时，先[选择存储](kubernetes_zh.md#3-部署与更新代码)：

```bash
foretoken deploy examples/quickstart/qwen3-0.6b -e . --timeout 20m
```

不传 `--name` 时，实例名取自 [`frontend.yaml`](../../examples/quickstart/qwen3-0.6b/frontend.yaml) 中 `FrontendService` 的 `metadata.name`，本例为 `qwen3-0-6b`。添加 `--name candidate-a` 则使用 `candidate-a`，后续操作也用这个名字。

实例在当前 kubeconfig 的命名空间中运行，包含前端和 `Qwen/Qwen3-0.6B` 模型服务，使用一张 GPU。部署完成后，发送请求：

```bash
FRONTEND_URL="$(foretoken endpoint qwen3-0-6b)"
REQUEST_HOST="$(foretoken endpoint qwen3-0-6b --host)"
curl --fail-with-body --no-buffer "$FRONTEND_URL/v1/chat/completions" \
  -H "Host: $REQUEST_HOST" \
  -H 'Content-Type: application/json' \
  -d '{"model":"Qwen/Qwen3-0.6B","messages":[{"role":"user","content":"Hello"}],"stream":true}'
```

修改控制器、前端或模型服务的 Go、Rust、Python 代码后，重新执行上面的部署命令，即可更新选中的实例，复用未变化的运行环境。共享 CRD 或平台配置通过 `foretoken install -e .` 更新。服务代码更新时会重启相应进程，并可能重新加载模型权重。

从其他目录执行时，可用 `-e` 指定 Foretoken 目录。例如，在仓库的父目录中执行 `foretoken deploy foretoken/examples/quickstart/qwen3-0.6b -e foretoken --timeout 20m`。部署配置的路径始终相对于执行命令的目录。

CLI 使用 editable 安装时选定的源码目录；修改其 Python 依赖后，需要重新安装 CLI。

示例使用完后删除：

```bash
foretoken delete qwen3-0-6b
```

### 并行运行候选方案

为每个候选使用独立的源码目录和实例名，即可在同一个 Kubernetes 命名空间中并行运行不同代码版本。候选共用已安装的平台，各自更新自己的控制器和服务代码，默认通过同一个 Gateway 提供 HTTP 访问。

运行环境相同的候选直接引用同一组运行时和编译器镜像，各自保留代码与可写构建状态。同一节点的镜像缓存由这些实例共享，不需要为每个 Agent 复制镜像；不同节点仍需获取本节点尚未缓存的镜像。

从仓库根目录执行：

```bash
git worktree add ../candidate-a -b experiment/candidate-a
git worktree add ../candidate-b -b experiment/candidate-b

foretoken deploy ../candidate-a/examples/quickstart/qwen3-0.6b \
  --name candidate-a -e ../candidate-a --timeout 20m
foretoken deploy ../candidate-b/examples/quickstart/qwen3-0.6b \
  --name candidate-b -e ../candidate-b --timeout 20m

foretoken perf ../candidate-a/examples/quickstart/qwen3-0.6b \
  --name candidate-a --num-prompts 20 \
  --output experiment --output-dir results/candidates --iteration candidate-a
```

候选沿用快速开始的单卡配置。平台已准备默认模型存储时，普通文件夹、clone 和 worktree 都会复用该存储，运行状态仍按实例隔离。自定义目录或独立动态存储见[模型存储](../features/model-storage_zh.md)。

每个候选分别需要 GPU、CPU 和内存。限定节点可添加 `spec.nodeSelector`，但它不指定 GPU 编号。

上述评测将结果保存在 `results/candidates` 下。[实验记录手册](../../rsi-playbooks/experiment-records_zh.md#分析结果)说明了比较候选时应查看哪些文件。共享 CPU、存储和网络的活动仍可能影响测量。GPU 全部占用时，替换服务可能需要短暂停止旧服务，释放设备。

候选结束后删除部署：

```bash
foretoken delete candidate-a
foretoken delete candidate-b
```

删除一个候选只移除该实例及其控制器，不影响旁边的候选或 Kubernetes 命名空间。源码目录、增量构建状态、实验结果和管理员提供的存储保留；模型缓存与动态卷的清理规则见 [CLI 指南](../../cli/README_zh.md#清理)。

## 修改推理引擎

要使用本地修改的 vLLM，部署时指定它的目录。该代码需与已安装运行环境的 Python、PyTorch 和设备库匹配。假设 vLLM 位于 `../vllm`：

```bash
foretoken deploy examples/quickstart/qwen3-0.6b --engine-source ../vllm --timeout 20m
```

后续从这个 Foretoken 目录部署时，会沿用选定的 vLLM 目录。修改 Python、Triton 或 NVIDIA CUDA/C++ 代码后，部署时添加 `-e .`，即可自动构建并应用改动。每个 Foretoken 源码目录的引擎目录需分别指定。

需要指定构建并发时，在源码安装或部署命令前加 `BUILD_JOBS=4`，各 Go、Rust、引擎原生编译和 Python 字节码准备任务分别使用四个并发作业。

修改沐曦原生 kernel 时，还需同时指定插件目录及与其匹配的 vLLM 目录：

```bash
foretoken deploy examples/quickstart/qwen3-0.6b \
  --engine-source ../vllm \
  --engine-source vllm-metax=../vllm-metax --timeout 20m
```

### 更换运行环境

普通部署不需要修改基础镜像。默认镜像包含运行模型服务所需的 Python、PyTorch、CUDA 和 vLLM。

如果需要使用自定义环境，修改 [`deploy/inference-engines/vllm/source-environment.json`](../../deploy/inference-engines/vllm/source-environment.json) 中的 `baseImage`：

```json
{"baseImage": "ghcr.io/example/custom-vllm:latest"}
```

源码安装、`make image-model-server`、`deploy/dev-build` 和发布构建都会使用这个设置。镜像必须与 Foretoken 的 vLLM 适配器兼容，并且构建器可以访问。已有源码安装修改后，部署时添加 `-e .`。

只为某次安装指定不同基础镜像时，在 `deploy/platform-values.yaml` 中设置 `runtime.vllm.image`：

```yaml
runtime:
  vllm:
    image: ghcr.io/example/custom-vllm:latest
```

重新执行安装命令，传入 `--values deploy/platform-values.yaml`，并保留镜像仓库和引擎源码选项。使用 `-e` 时，Foretoken 以此镜像为构建基础；将该字段设为 `auto` 可恢复自动选择。安装完成后重新部署工作负载。

沐曦基础镜像构建见[准备沐曦 Foretoken 平台](metax-platform_zh.md#从源码安装)。

### vLLM-Omni 运行时

vLLM-Omni 使用独立的模型服务镜像。在具备 Docker BuildKit 和 Make 的机器上，从仓库根目录构建：

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

远程集群沿用 [Kubernetes 部署指南](kubernetes_zh.md) 中的 `REGISTRY` 设置；仓库需要认证时，先完成登录，再推送模型服务镜像：

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
