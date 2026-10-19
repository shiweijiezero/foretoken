<!--
SPDX-License-Identifier: Apache-2.0
SPDX-FileCopyrightText: Copyright contributors to the Foretoken project
-->

# Foretoken 命令行工具

[English](README.md) | 简体中文

Foretoken 命令行工具通过统一的 `foretoken` 入口安装 Kubernetes 平台、从 Kustomize 配置部署模型服务、查看服务就绪状态、解析前端访问入口并运行评测。

## 开始前

准备 Python 3.11 或更高版本、`kubectl` 和目标集群的访问配置。安装平台或开发源码时，还需 Helm 4.2.4 或更高版本。可以按下文创建本地集群，或选择已有 Kubernetes context。GPU 节点需要安装厂商驱动和 Kubernetes device plugin。

## 安装命令行工具

使用 pip 安装发布的命令行工具：

```bash
pip install foretoken

# 从源码目录安装：
# pip install -e .
```

或使用 uv 创建并激活虚拟环境后安装：

```bash
uv venv
source .venv/bin/activate
uv pip install foretoken
```

运行 `foretoken --version` 查看已安装的 CLI 版本。

## 创建本地集群

在已安装 Docker、NVIDIA Container Toolkit 和 k3d 的 Linux GPU 主机上创建本地 GPU 集群：

```bash
# 为本地集群命名，并使用 nvidia-smi 显示的 GPU 编号 0。
# 若使用两张 GPU，传入 --gpus 0,1。
foretoken cluster create k3d --name foretoken-dev --gpus 0
```

创建本地 kind 开发集群时，安装 Docker、kind、kubectl 和 Helm：

```bash
foretoken cluster create kind --name foretoken-dev
```

删除由 CLI 创建的集群：

```bash
foretoken cluster delete k3d --name foretoken-dev
foretoken cluster delete kind --name foretoken-dev
```

## 安装 Kubernetes 平台

管理员在集群中执行一次 `foretoken install`，准备共享 API、依赖、监控和部署默认配置。平台资源使用 `foretoken-platform` 命名空间，网关模式还会准备共享访问入口。

用户随后通过 `foretoken deploy` 创建独立部署实例。平台更新不会重启已有实例；重新部署某个实例后，它才会采用更新后的默认配置。

### 默认安装

默认安装已发布的平台，并为模型部署选择通过 `LoadBalancer` Service 直接访问的方式：

```bash
foretoken install
```

安装会自动选择 NVIDIA 或沐曦运行时，并复用或安装 LeaderWorkerSet 控制器和 RDMA 设备插件。`--values` 中显式指定的运行时配置优先。混合 GPU 集群通过 `runtime.vllm.gpu.resourceName` 选择资源，或通过 `runtime.vllm.gpu.nodeSelector` 限定节点范围。

安装默认启用日志采集和持久化。配置、日志查询、看板和告警见[可观测性](../observability/README_zh.md)。

### 网关模式

网关模式会创建专用的 `GatewayClass` 和 `Gateway`，集群没有可复用的控制器时自动安装 Envoy Gateway：

```bash
foretoken install --frontend-mode gateway
```

使用其他 Gateway Controller 时，应复用由该 Controller 管理的 Gateway：

```bash
foretoken install \
  --frontend-mode gateway \
  --gateway-name inference-gateway \
  --gateway-namespace gateway-system
```

只有多个 listener 都符合条件时，才需要添加 `--gateway-section-name LISTENER`。

### 当前源码

集群需为应用文件提供默认存储类（StorageClass）。在 Foretoken 仓库根目录运行：

```bash
foretoken install -e .
```

部署时添加 `-e .`，即可从当前 Foretoken 目录构建控制器、前端和模型服务的改动。并行运行不同代码版本，见[多版本部署示例](../docs/deployment/source_zh.md#并行运行候选方案)；使用本地 vLLM 或沐曦插件，见[引擎源码部署](../docs/deployment/source_zh.md#修改推理引擎)。

当前 context 是标准 kind 或 k3d 时，命令直接在节点载入构建好的镜像；其他 Kubernetes context 需要构建 Pod 和节点都能访问的镜像仓库。使用无认证的内网仓库时：

```bash
foretoken install -e . --registry registry.example.com:5000/foretoken
```

如果仓库需要认证，安装前按[源码部署指南](../docs/deployment/kubernetes_zh.md)配置拉取 Secret。

### 模型分发

使用 Dragonfly 在节点间共享公开模型文件时，在 `deploy/platform-values.yaml` 中配置：

```yaml
modelDistribution:
  dragonfly:
    enabled: true
```

使用已发布平台时，执行：

```bash
foretoken install --values deploy/platform-values.yaml
```

源码安装从仓库根目录执行 `foretoken install -e . --values deploy/platform-values.yaml`，保留原镜像仓库和引擎源码选项。

安装命令会准备 Dragonfly，或复用已有安装。需要身份认证的模型及自定义模型源仍直接从源站下载。要选择特定的 Dragonfly Helm release，在 `modelDistribution.dragonfly` 下设置 `existingRelease: {name: dragonfly, namespace: dragonfly-system}`。

在具备 RDMA 的 NVIDIA 集群上，还可以通过 ModelExpress 从运行中的副本加载权重。在同一个 `modelDistribution` 下加入：

```yaml
modelDistribution:
  modelexpress:
    enabled: true
```

自动权重传输适用于使用持久缓存、数据并行度为 1、未启用专家动态负载均衡的远端模型；显式设置的 `load-format` 保持不变。每个 GPU worker 选择拓扑邻近的可用 RDMA 网卡，没有兼容源副本时加载已准备的文件。

修改设置后重新执行上述安装命令，再部署需要更新的服务。将 `enabled` 设为 `false` 即可关闭对应能力。`foretoken uninstall` 在没有工作负载使用时移除托管的 Dragonfly，复用的安装会保留。

### 安装选项

需要自定义平台设置时，使用 `--values`。没有显式覆盖时，每次安装会为默认平台镜像和 Chart 下载选择公共来源。自动选中的代理失败时，Chart 下载和源码构建中的镜像拉取可回退到源站。通过 `--oci-registry` 指定仓库，values 中明确填写的镜像地址保持不变。选源在运行 CLI 的机器上执行，所选仓库也需要能从集群节点访问。

模型服务通过一个集群外可访问的 IP 提供服务。k3d、k3s 和云上集群会自动分配这个 IP；用 kubeadm、RKE2 或 kubespray 搭建的集群默认没有地址分配能力，安装结尾会提示 `LoadBalancer support Not verified`。此时向集群管理员确认一段节点网段内未被占用的 IP 交给 Foretoken，由它分配给服务：

```yaml
loadBalancer:
  managedAddresses:
    - 192.168.1.240-192.168.1.250
```

## 为成员准备命名空间

平台安装后，管理员在已有命名空间内分配服务操作权限。将 `team-inference` 换成实际命名空间，`MEMBER_ID` 换成成员 kubeconfig 对应的 Kubernetes 身份：

```bash
kubectl create rolebinding foretoken-operator --namespace team-inference \
  --clusterrole foretoken-operator --user MEMBER_ID
```

安装平台不会自动给用户授权。这条 RoleBinding 让成员能够部署、查看、暂停和删除实例，无需管理共享平台。使用 `deploy -e` 开发源码仍需单独授予构建权限。

成员在自己的 kubeconfig 中选择一次分配的命名空间：

```bash
kubectl config set-context --current --namespace team-inference
```

日常命令会自动使用该命名空间，无需创建 Namespace 资源或安装控制器。部署时，平台会准备该实例的控制器和已授权存储。管理员选择共享目录的方式见[模型存储](../docs/features/model-storage_zh.md#跨命名空间复用模型文件)。

## 部署和管理模型服务

从[快速开始](../README_zh.md)准备的仓库目录执行，部署一个 Kustomize 根目录中的前端服务和全部模型。

资源要求见[多模型示例](../examples/quickstart/multi-model/README_zh.md)，目录和 PVC 配置见[模型存储](../docs/features/model-storage_zh.md)。单模型部署使用 `examples/quickstart/qwen3-0.6b`。

```bash
foretoken deploy examples/quickstart/multi-model --name team-models --timeout 20m
```

命令在当前 kubeconfig 的命名空间中创建或更新 `team-models` 部署实例，每个实例都有自己的控制器和服务。对同一目录使用另一个 `--name`，即可并行运行独立实例；更新、暂停和删除只作用于选中的实例。

不传 `--name` 时，默认使用配置中唯一 FrontendService 的名称，本例为 `multi-model-frontend`；前端资源也使用该实例名。普通部署直接使用已安装的应用，不构建源码。只有开发源码时才添加 `-e PATH`。

等待期间显示服务状态和 Pod/容器日志。运行中的服务达到 Ready、零副本服务停止、所选告警配置完成后，命令退出。告警配置见[服务可观测性示例](../examples/feature/observability/README_zh.md)。

`--timeout` 设置 CLI 等待集群操作和服务就绪的时限，默认为十分钟，源码编译单独执行。模型的准备和启动分别受模型配置中的 `spec.timeouts.startup` 约束。

模型准备失败或就绪等待超时时，命令会给出原因并退出，已经部署的服务仍保留在 Kubernetes 中。集群会继续尝试恢复，可用下面的 `status` 命令跟踪进度、查看日志。若是配置错误，修改 Kustomize 文件后重新执行 `deploy`。

不应用配置，直接查看同一部署的状态：

```bash
foretoken status team-models
```

状态输出包含 Kubernetes 的等待与退出消息，重启后也保留上一次容器退出的诊断。可以列出当前命名空间中的实例，或持续查看指定实例的状态和 Pod/容器日志，按 Ctrl+C 结束：

```bash
foretoken status
foretoken status team-models --watch
```

这些命令也接受 Kustomize 路径。通过路径选择具名实例时，同时传入 `--name team-models`，例如 `foretoken status examples/quickstart/multi-model --name team-models`。

部署后解析前端服务的公开 URL：

```bash
FORETOKEN_FRONTEND_URL="$(foretoken endpoint team-models)"
```

HTTP Gateway 模式下，单独解析请求的 `Host`：

```bash
FORETOKEN_REQUEST_HOST="$(foretoken endpoint team-models --host)"
```

直接访问时，`--host` 返回主机名或 IP，以及 URL 中包含的端口；HTTP Gateway 模式下返回配置的路由域名。`foretoken endpoint` 等待 LoadBalancer 或 Gateway 分配地址；要等待服务就绪，请使用 `foretoken deploy`。

### 停止与恢复

```bash
foretoken stop team-models
```

实验暂时不用 GPU 时，可以暂停实例，同时保留配置、模型缓存引用和运行状态。命令等待该实例的前端和模型退出；之后执行 `foretoken deploy examples/quickstart/multi-model --name team-models` 即可恢复，模型进程会重新启动并加载权重。

## 性能评测：响应速度与吞吐量

使用 `foretoken perf` 测量响应延迟、请求吞吐量和 token 生成速度。传入 Kustomize 目录，或用 `--url` 和 `--model` 指定已有服务。评测上面的具名实例时，添加 `--name team-models`；`eval` 使用相同的选择方式。各类负载见[性能评测示例](../benchmarks/docs/perf/README_zh.md)。

## 质量评测：回答评分与模型对比

使用 `foretoken eval`，通过 lm-evaluation-harness 或 EvalScope 为模型回答评分。服务选择方式与性能评测相同，任务和判分参数采用所选框架的写法。具体命令见[质量评测](../benchmarks/docs/eval/README_zh.md)。添加 `--reference` 可[比较候选与参考模型的概率分布](../benchmarks/docs/eval/distribution-comparison_zh.md)。

## 导出图表

评测时用 `--output plot` 导出图表，已有结果用 `foretoken plot RESULT_DIR [RESULT_DIR ...]` 重绘或对照。指标、图宽和输出位置见[绘图示例](../benchmarks/docs/recipes_zh.md#绘制与对照结果)。

添加 [`--sync_wandb`](../benchmarks/docs/perf/wandb_zh.md#恢复上传) 可将已有结果上传到 W&B。

## 性能剖析：执行瓶颈

在 `foretoken deploy` 或 `foretoken perf` 后加 `--profile`，记录 CPU/GPU 执行过程，再用 `foretoken profile view` 浏览结果。环境准备与采集命令见[性能剖析](../benchmarks/docs/profile/README_zh.md)。

## 清理

删除部署的服务：

```bash
foretoken delete team-models
```

共享平台不再使用时，由平台管理员执行 `foretoken uninstall`。

删除命令等待选中实例的服务和工作负载退出，再移除其控制器和实例资源，不删除 Kubernetes 命名空间、相邻实例或管理员提供的存储。托管模型下载在最后一个服务引用释放后回收，`stop` 则保留引用。动态卷遵循配置的保留策略及 StorageClass 回收策略。

卸载保留 CRD、日志存储和复用的集群组件。仍有工作负载依赖托管的 LeaderWorkerSet 或 MetalLB 时，也会保留对应控制器。
