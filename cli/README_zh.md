<!--
SPDX-License-Identifier: Apache-2.0
SPDX-FileCopyrightText: Copyright contributors to the Foretoken project
-->

# Foretoken 命令行工具

[English](README.md) | 简体中文

通过 `foretoken` 部署模型服务、查看运行状态、执行评测和采集性能数据。首次部署见[快速开始](../README_zh.md#快速开始)；使用 Agent 持续优化推理系统，见 [RSI 任务](../rsi-playbooks/README_zh.md#开始优化任务)。

| 用途 | 命令 |
| --- | --- |
| [环境与平台](#环境与平台) | `cluster create`、`cluster delete`、`install`、`uninstall` |
| [服务部署](#服务部署) | `deploy`、`stop`、`delete`、`endpoint` |
| [状态与日志](#状态与日志) | `status` |
| [评测与结果分析](#评测与结果分析) | `perf`、`eval`、`plot` |
| [性能剖析](#性能剖析) | `deploy --profile`、`perf --profile`、`profile view` |

`foretoken --help` 列出全部命令。查看完整选项时，在具体命令后加 `--help`，例如 `foretoken status --help`。

## 开始前

### 安装命令行工具

```bash
uv venv --python 3.12 --seed  # 使用 pip 时：python3 -m venv .venv（Python 3.11+）
source .venv/bin/activate
uv pip install foretoken  # 或 pip install foretoken
foretoken --version
```

运行 `foretoken` 和 `wandb` 时保持环境激活。

部署和本地集群示例使用仓库中的配置文件。获取仓库后，从仓库根目录执行这些示例：

```bash
git clone https://github.com/shiweijiezero/foretoken.git
cd foretoken

# 开发 CLI 时：
# uv pip install -e .  # 或 pip install -e .
```

部署服务需要 `kubectl` 和目标集群的 kubeconfig。安装平台或开发源码时，还需 Helm 4.2.4 或更高版本。平台和服务命令会始终使用启动时选定的 Kubernetes context。评测已有 URL、绘制本地结果时无需 Kubernetes。

## 环境与平台

### cluster create / cluster delete

创建本地开发集群，或在不再使用时删除。已有 Kubernetes 集群可以直接使用。

```bash
# 在 Linux 主机创建 GPU 集群，选择物理 GPU 0
foretoken cluster create k3d --name foretoken-dev --gpus 0

# 创建以 CPU 为主的开发集群
foretoken cluster create kind --name foretoken-dev
```

创建后会切换到新集群的 Kubernetes context。k3d 方式需要 Docker、NVIDIA 驱动和 Container Toolkit、k3d；kind 方式需要 Docker 和 kind。两者均可用 `--config` 指定自定义集群配置。宿主机准备见 [k3d 指南](../docs/deployment/k3d_zh.md)和 [kind 指南](../docs/deployment/kind_zh.md)。

删除整个集群及其中的工作负载：

```bash
foretoken cluster delete k3d --name foretoken-dev
```

删除 kind 集群时，将 `k3d` 换为 `kind`。仓库中的 `data/` 目录会保留。

<a id="安装-kubernetes-平台"></a>
<a id="当前源码"></a>

### install

管理员在集群中安装一次 Foretoken 共享平台，在 `foretoken-platform` 命名空间准备 API、依赖、监控、持久日志和部署默认配置。用户随后通过 `foretoken deploy` 创建独立实例。

```bash
# 安装发布版平台
foretoken install

# 从当前 Foretoken 仓库源码构建
foretoken install -e .
```

集群需提供默认 StorageClass，GPU 节点需具备厂商驱动和设备插件。新安装默认共用一个 Gateway 入口；使用 `--frontend-mode local` 可为每个实例提供独立的 `LoadBalancer` Service。平台更新不会重启已有实例；重新部署某个实例后，它才会采用更新后的默认配置。

复用已有 Gateway 时，指定 `--frontend-mode gateway`、`--gateway-name` 和 `--gateway-namespace`；需要选择监听器时，再添加 `--gateway-section-name`。自定义域名和 HTTPS 见[网关配置](../README_zh.md#网关模式)。

`--values` 应用 [Helm 配置](../deploy/charts/foretoken/values.yaml)，`--oci-registry` 指定默认镜像和 Chart 的仓库前缀。监控设置见[可观测性](../observability/README_zh.md#平台设置)，节点间模型分发见[模型存储](../docs/features/model-storage_zh.md#在节点间分发模型)。

新建的托管 Gateway 默认在 8081 端口监听 HTTP。需要使用其他监听端口时，在 `deploy/platform-values.yaml` 中添加以下配置，并在安装命令中传入 `--values deploy/platform-values.yaml`：

```yaml
frontend:
  gateway:
    port: 8082
```

源码构建时，标准 kind、k3d context 可直接载入镜像，其他集群需指定 `--registry`。显式执行 `deploy -e PATH` 只更新目标实例的控制器和服务应用；新工作站可以直接使用已安装的源码版平台，不必重新安装。准备步骤、引擎源码和并行候选用法见[源码部署](../docs/deployment/source_zh.md)。

### 为成员准备命名空间

平台安装后，管理员在已有命名空间内分配服务操作权限。将 `team-inference` 换成实际命名空间，`MEMBER_ID` 换成成员 kubeconfig 对应的 Kubernetes 身份：

```bash
kubectl create rolebinding foretoken-operator --namespace team-inference \
  --clusterrole foretoken-operator --user MEMBER_ID
```

这条 RoleBinding 允许成员部署、查看、暂停和删除实例。源码构建命令 `deploy -e` 需要平台级权限，由管理员或获授权的可信开发者执行。

成员在自己的 kubeconfig 中选择一次分配的命名空间：

```bash
kubectl config set-context --current --namespace team-inference
```

日常命令会自动使用该命名空间，无需创建 Namespace 资源或安装控制器。部署时，平台会准备各实例的控制器和已授权存储，配置见[共享目录](../docs/features/model-storage_zh.md#跨命名空间复用模型文件)。

### uninstall

部署实例全部删除后，由平台管理员卸载共享平台及其托管的开发存储。Kubernetes 集群、Foretoken CRD、已保存的日志和复用的集群组件会保留。

```bash
foretoken uninstall
```

<a id="部署和管理模型服务"></a>

## 服务部署

从 Kustomize 目录部署一个前端及其模型，实例使用当前 kubeconfig 的命名空间。各实例拥有独立的控制器和服务，更新、暂停和删除不会影响同一命名空间中的相邻实例。

下面使用[单模型快速开始](../examples/quickstart/qwen3-0.6b/README_zh.md)，资源要求和配置说明见该示例。不传 `--name` 时，实例名取自 [`frontend.yaml`](../examples/quickstart/qwen3-0.6b/frontend.yaml) 中 `FrontendService` 的 `metadata.name`，本例为 `qwen3-0-6b`。下文通过 `--name qwen` 指定实例名，后续操作也使用 `qwen`。

### deploy

创建、更新或恢复实例，并等待服务达到配置要求的状态：

```bash
foretoken deploy examples/quickstart/qwen3-0.6b --name qwen --timeout 20m
```

对同一份配置使用另一个 `--name`，即可运行独立实例。普通部署直接使用已安装的应用，不构建源码。只有在源码版平台上开发代码时，才添加 `-e PATH`。

修改配置后重新执行。`--timeout` 设置 CLI 等待时限，不改变模型的 `spec.timeouts.startup`；源码编译单独执行。准备失败或就绪等待超时后，服务仍保留在集群中。通过 `status` 查看原因，必要时修正配置并重新部署。

### stop

暂停选中的实例并释放 GPU，保留配置、模型缓存引用和运行状态。重新执行该实例的部署命令即可恢复。

```bash
foretoken stop qwen
```

<a id="清理"></a>

### delete

只删除选中的实例。命令等待其服务和工作负载退出，再移除该实例的控制器与资源，不删除 Kubernetes 命名空间、相邻实例或管理员提供的存储。

```bash
foretoken delete qwen
```

开发环境和增量构建状态会保留，供后续源码部署复用。托管模型下载在最后一个服务引用释放后回收，`stop` 则保留这些引用。动态卷遵循保留策略和 StorageClass 回收策略，详见[模型存储](../docs/features/model-storage_zh.md#暂停删除与复用)。

### endpoint

输出前端 URL；添加 `--host` 则输出请求所需的 Host。HTTP Gateway 路由会按实例自动配置。自定义域名、HTTPS 或限定域名的监听器，见[网关配置](../README_zh.md#网关模式)。

```bash
# 公开访问 URL
foretoken endpoint qwen

# HTTP Host 取值
foretoken endpoint qwen --host
```

该命令等待访问地址分配，服务就绪由 `deploy` 等待。客户端用法见[发送请求](../README_zh.md#4-发送测试请求)。

## 状态与日志

### status

查看服务就绪情况、启动进度和 Pod/容器失败原因。可以选择实例、列出当前命名空间中的实例，或持续跟踪指定实例的状态和日志：

```bash
foretoken status qwen
foretoken status
foretoken status qwen --watch
```

`status`、`stop`、`delete` 和 `endpoint` 也接受 Kustomize 路径。通过路径选择具名实例时，同时传入原来的 `--name`，例如 `foretoken status examples/quickstart/qwen3-0.6b --name qwen`。

按 Ctrl+C 结束查看，服务继续运行。持久日志、指标和告警见[可观测性指南](../observability/README_zh.md)。

## 评测与结果分析

`perf` 和 `eval` 接受部署目录或已有 API 地址，用部署时的 `--name` 选择具名实例。部署包含多个模型时，用 `--model` 指定公开模型标识。

评测复用已有服务，测量配置或源码改动前先执行 `deploy`。实例不存在时，会基于已安装的应用临时启动服务，并在评测结束后回收本次创建的运行资源。

### perf

在指定负载下测量响应延迟、请求吞吐量和 token 吞吐量：

```bash
# 评测具名 Foretoken 实例
foretoken perf examples/quickstart/qwen3-0.6b --name qwen --num-prompts 20 --output local

# 评测已有 API；按实际服务替换 URL 和模型标识
foretoken perf --url http://localhost:8000/v1/chat/completions \
  --model Qwen/Qwen3-0.6B --prompt Hello --num-prompts 20 --output local
```

URL 方式需要完整请求地址，并显式提供提示词或数据集。`--output local` 将每次结果保存到 `results/` 下，无需 W&B 账号。数据集、并发数、请求到达速率和参数扫描见[性能评测](../benchmarks/docs/perf/README_zh.md)。

### eval

通过 lm-evaluation-harness 或 EvalScope 为模型回答评分。下面评测 100 道 GSM8K 数学题：

```bash
foretoken eval examples/quickstart/qwen3-0.6b --name qwen \
  --evaluator lm-eval --tasks gsm8k --limit 100 --output local
```

任务参数沿用所选框架的语法。其他任务和 EvalScope 用法见[质量评测](../benchmarks/docs/eval/README_zh.md)；用 `--reference` 比较候选模型与参考模型，见[分布对比](../benchmarks/docs/eval/distribution-comparison_zh.md)。

### plot

从已保存的评测结果生成图表，无需重新运行推理。绘制前面示例保存的结果：

```bash
foretoken plot results
```

图表写入 `results/plots/`。传入具体运行目录，可选择或对比特定结果。更多用法见[绘图选项](../benchmarks/docs/recipes_zh.md#绘制与对照结果)和 [W&B 结果补传](../benchmarks/docs/perf/wandb_zh.md#恢复上传)。

## 性能剖析

### deploy --profile / perf --profile

记录模型的 CPU/GPU 执行时间线。`deploy --profile` 在服务就绪后采集外部请求，`perf --profile` 则自行生成评测负载。

```bash
# 采集应用或另一个终端发来的请求
foretoken deploy examples/quickstart/qwen3-0.6b --name qwen \
  --profile --profile-engine pytorch --profile-duration 15s --timeout 20m

# 发送评测请求并采集执行过程
foretoken perf examples/quickstart/qwen3-0.6b --name qwen \
  --profile --profile-engine pytorch --profile-duration 15s \
  --num-prompts 2 --max-tokens 128 --output local
```

采集需要 Foretoken 部署目录，并同时指定 `--profile-engine` 和 `--profile-duration`。支持 `pytorch`、NVIDIA `nsight` 和沐曦 `mctracer`。快速开始示例已提供持久化采集存储，各工具的准备见[性能剖析指南](../benchmarks/docs/profile/README_zh.md)。

### profile view

浏览保存在集群中的采集结果。在使用浏览器的电脑上配置目标集群的 kubeconfig，然后运行：

```bash
foretoken profile view
```

打开打印的本地 URL。添加 `-n team-inference` 可只在该命名空间查找结果。按 Ctrl+C 关闭查看器，模型服务和采集结果会保留。
