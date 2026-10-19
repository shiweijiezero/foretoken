<!-- SPDX-License-Identifier: Apache-2.0 -->
<!-- SPDX-FileCopyrightText: Copyright contributors to the Foretoken project -->

# 从源码部署 Foretoken

[English](source.md) | [中文](source_zh.md)

从本地源码构建 Foretoken，并将源码改动部署到 Kubernetes 集群。

## 从源码安装

准备 Python 3.11+、Git、kubectl 和 [Helm 4](../../cli/README_zh.md#开始前)，选择有 GPU 和默认存储类（StorageClass）的 Kubernetes 集群。共享集群的[源码构建权限](../../cli/README_zh.md#为成员准备命名空间)由管理员分配。

```bash
git clone https://github.com/shiweijiezero/foretoken.git
cd foretoken
kubectl config current-context
```

从当前源码安装 CLI：

```bash
pip install -e .
```

集群只需安装一次源码版平台。平台已经从源码安装时，可直接进入[部署步骤](#部署模型服务)；另一台工作站只需安装 CLI 并准备源码目录。

首次在本地 kind 或 k3d 集群安装时，无需镜像仓库，可直接构建并安装：

```bash
foretoken install -e .
```

这会安装共享的 Foretoken 平台，构建在集群中进行；也可替换已通过 CLI 安装的发布版平台。k3d 的 GPU 配置见[使用 k3d 部署 Foretoken](k3d_zh.md)。其他 Kubernetes 集群需要构建任务和节点都能访问的镜像仓库，安装命令及仓库认证见[Kubernetes 部署指南](kubernetes_zh.md#2-从源码安装-foretoken)。

源码构建自动复用节点本地的持久编译缓存。需要将构建产物存入其他存储类时，在 `deploy/platform-values.yaml` 中设置 `development.build.storageClassName`，通过 `--values` 传入；执行和编译缓存仍由框架自动安排在本地存储。支持动态供卷的原生 NFS 和 `nfs.csi.k8s.io`。应用文件使用集群默认存储类，也可通过 `applicationFiles.storageClassName` 指定。已有卷保留其存储类。

## 部署模型服务

在 Foretoken 仓库根目录部署快速开始示例，使用当前目录的代码：

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

修改控制器、前端或模型服务代码后，重新执行上面的部署命令，即可构建改动并只更新该实例，不影响同一命名空间中的其他实例。共享 CRD 或平台配置的变更仍通过 `foretoken install -e .` 应用。服务代码更新时会重启相应进程，并可能重新加载模型权重。

从其他目录执行时，可用 `-e` 指定 Foretoken 目录。例如，在仓库的父目录中执行 `foretoken deploy foretoken/examples/quickstart/qwen3-0.6b -e foretoken --timeout 20m`。部署配置的路径始终相对于执行命令的目录。

CLI 代码使用执行 `pip install -e .` 时的目录；修改其 Python 依赖后，需要重新安装 CLI。

示例使用完后删除：

```bash
foretoken delete qwen3-0-6b
```

### 并行运行候选方案

为每个候选使用独立的源码目录和实例名，即可在同一个 Kubernetes 命名空间中并行运行不同代码版本。候选共用已安装的平台，各自更新自己的控制器和服务代码。

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

候选沿用快速开始的单卡配置，默认 `data/` 会指向主 checkout 的数据目录，复用模型下载，同时为各实例分别保存运行状态。目录需要能被目标节点访问，配置见[共享模型存储](../features/model-storage_zh.md#跨命名空间复用模型文件)；没有共享目录时，可改用[动态存储](../features/model-storage_zh.md#使用动态存储)。

每个候选分别需要 GPU、CPU 和内存。限定节点可添加 `spec.nodeSelector`，但它不指定 GPU 编号。

上述评测将结果保存在 `results/candidates` 下。[实验记录手册](../../rsi-playbooks/experiment-records_zh.md#分析结果)说明了比较候选时应查看哪些文件。共享 CPU、存储和网络的活动仍可能影响测量。GPU 全部占用时，替换服务可能需要短暂停止旧服务，释放设备。

候选结束后删除部署：

```bash
foretoken delete candidate-a
foretoken delete candidate-b
```

删除一个候选只移除该实例及其控制器，不影响旁边的候选或 Kubernetes 命名空间。源码目录、实验结果和管理员提供的存储保留；模型缓存与动态卷的清理规则见 [CLI 指南](../../cli/README_zh.md#清理)。

## 修改推理引擎

要使用本地修改的 vLLM，部署时指定它的目录。该代码需与已安装运行环境的 Python、PyTorch 和设备库匹配。假设 vLLM 位于 `../vllm`：

```bash
foretoken deploy examples/quickstart/qwen3-0.6b --engine-source ../vllm --timeout 20m
```

后续从这个 Foretoken 目录部署时，会沿用选定的 vLLM 目录。修改 Python、Triton 或 NVIDIA CUDA/C++ 代码后，部署时添加 `-e .`，即可自动构建并应用改动。每个 Foretoken 源码目录的引擎目录需分别指定。

需要限制引擎原生编译并发时，在部署命令前加 `BUILD_JOBS=4`，使用四个编译作业。该选项不设置 Go/Rust 编译并发或内存上限。

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
