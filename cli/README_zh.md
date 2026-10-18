<!--
SPDX-License-Identifier: Apache-2.0
SPDX-FileCopyrightText: Copyright contributors to the Foretoken project
-->

# Foretoken 命令行工具

[English](README.md) | 简体中文

`foretoken` 用于准备 Kubernetes 环境、管理模型服务、评测性能与质量，以及采集执行时间线。首次部署可按[快速开始](../README_zh.md#快速开始)完成从安装到发送请求的完整流程。

| 类别 | 命令 | 什么时候用 |
| --- | --- | --- |
| [1. 环境与平台](#1-环境与平台) | `foretoken cluster create` | 在本机创建 k3d GPU 集群或 kind 开发集群。 |
| | `foretoken install` | 安装或更新 Foretoken 共享 API、依赖和部署默认配置。 |
| | `foretoken uninstall` | 删除模型部署后，卸载 Foretoken 平台。 |
| | `foretoken cluster delete` | 删除本地集群及其中的工作负载。 |
| [2. 服务部署](#2-服务部署) | `foretoken deploy` | 创建、更新或恢复部署目录中定义的服务。 |
| | `foretoken endpoint` | 获取部署的公开 URL 或 HTTP 路由域名。 |
| | `foretoken stop` | 暂停服务，释放运行资源，保留配置、存储和构建缓存。 |
| | `foretoken delete` | 删除部署及其关联资源。 |
| [3. 状态与日志](#3-状态与日志) | `foretoken status` | 查看就绪状态和失败原因，或用 `--watch` 持续跟踪状态与容器日志。 |
| [4. 评测与结果分析](#4-评测与结果分析) | `foretoken perf` | 在指定负载下测量延迟和吞吐量。 |
| | `foretoken eval` | 为模型回答评分，或比较候选模型与参考模型。 |
| | `foretoken plot` | 绘制、对比已保存的评测结果，或将结果上传到 W&B。 |
| [5. 性能剖析](#5-性能剖析) | `foretoken deploy --profile` / `foretoken perf --profile` | 处理外部请求或评测负载时，记录 CPU/GPU 执行过程。 |
| | `foretoken profile view` | 在本地网页查看当前及历史采集结果。 |

## 开始前

### 安装命令行工具

使用 Python 3.11 或更高版本：

```bash
pip install foretoken
foretoken --version
```

使用 uv 时，先通过 `uv venv` 和 `source .venv/bin/activate` 创建并激活虚拟环境，再执行 `uv pip install foretoken`。

`foretoken --help` 列出全部命令。查看具体参数时，在命令后加 `--help`，例如 `foretoken deploy --help` 或 `foretoken cluster create --help`。

### 准备示例与集群访问

下文的本地集群命令和部署示例会使用仓库中的文件。先获取仓库，再从仓库根目录执行这些命令：

```bash
git clone https://github.com/shiweijiezero/foretoken.git
cd foretoken
```

需要开发 CLI 本身时，在该目录执行 `pip install -e .`，从源码安装。

操作 Kubernetes 需要 `kubectl`、Helm 4.2.4 或更高版本，以及指向目标集群的 kubeconfig。已有集群可以直接使用，无需创建本地集群。GPU 节点需要厂商驱动和 Kubernetes 设备插件。评测已有 URL、绘制本地结果时无需 Kubernetes。

## 1. 环境与平台

### `cluster create`：创建本地环境

在已安装 Docker、NVIDIA Container Toolkit 和 k3d 的 Linux GPU 主机上执行：

```bash
foretoken cluster create k3d --name foretoken-dev --gpus 0
```

`--gpus` 使用 `nvidia-smi` 显示的物理 GPU 编号；两张卡可写为 `0,1`。命令会准备 NVIDIA 设备插件，并切换到新建集群的 Kubernetes context。宿主机准备见 [k3d 指南](../docs/deployment/k3d_zh.md)。

进行以 CPU 为主的平台开发时，安装 Docker 和 kind 后执行：

```bash
foretoken cluster create kind --name foretoken-dev
```

默认 kind 配置不提供 GPU，适用流程见 [kind 指南](../docs/deployment/kind_zh.md)。两种集群都可以通过 `--config` 指定自定义集群配置文件。

### 安装 Kubernetes 平台

用 `foretoken install` 准备模型部署需要的集群环境：

```bash
foretoken install
```

命令安装或更新 Foretoken 共享 API 和依赖，启用监控与持久日志，并选择 NVIDIA 或沐曦运行时。集群需提供用于持久存储的默认 StorageClass。前端默认通过 `LoadBalancer` 类型的 Service 提供访问入口。

更新平台时重新执行该命令，更新后的默认配置在服务重新部署后生效。安装平台后，用 `foretoken deploy` 部署具体模型。

### 当前源码

从当前仓库构建平台：

```bash
foretoken install -e .
```

之后执行 `foretoken deploy` 时，会从当前 Foretoken 目录构建控制器和服务代码的改动。标准 kind、k3d context 可直接向节点载入构建好的镜像；其他集群需要通过 `--registry` 指定构建任务和节点都可访问的镜像仓库。远程仓库、引擎源码改动和并行候选部署见[源码部署指南](../docs/deployment/source_zh.md)。

### 自定义安装

| 需要做什么 | 选项或指南 |
| --- | --- |
| 通过域名和 Gateway 访问 | `--frontend-mode gateway`，见[创建与复用网关](../docs/deployment/platform_zh.md#网关访问)。 |
| 选择镜像、GPU 资源或其他平台配置 | `--values`，见[平台配置](../docs/deployment/platform_zh.md#自定义平台配置)。 |
| 指定镜像与 Chart 来源 | `--oci-registry`，见[仓库选源](../docs/deployment/platform_zh.md#镜像与-chart-来源)。 |
| 配置监控、Grafana 登录或日志保留 | [可观测性](../observability/README_zh.md#平台设置)。 |
| 分配服务 IP 或共享模型下载 | [LoadBalancer 地址](../docs/deployment/platform_zh.md#分配-loadbalancer-地址)与[模型分发](../docs/deployment/platform_zh.md#用-dragonfly-共享模型下载)。 |

### `uninstall`：卸载平台

用 `foretoken delete` 删除全部模型部署后，再执行：

```bash
foretoken uninstall
```

卸载会移除平台及其托管依赖，保留 CRD、日志存储和复用的集群组件。仍被其他工作负载使用的托管 LeaderWorkerSet、MetalLB 和 Dragonfly 会保留，Kubernetes 集群本身继续存在。

### `cluster delete`：删除本地环境

本地集群不再使用时：

```bash
foretoken cluster delete k3d --name foretoken-dev
```

删除 kind 集群时，将 `k3d` 换为 `kind`。命令会删除集群和其中的工作负载，仓库中的 `data/` 目录会保留。

## 2. 服务部署

服务命令接收 Kustomize 目录，即包含 `kustomization.yaml` 及其引用资源的配置目录。一个部署包含同一命名空间中的一个 `FrontendService` 和一个或多个 `ModelService`。

下文使用[单模型快速开始](../examples/quickstart/qwen3-0.6b/README_zh.md)，工作负载合计请求 1 张 GPU、8 个 CPU 和 52 GiB 内存，平台还需额外资源。同一前端下运行多个模型，见[多模型示例](../examples/quickstart/multi-model/README_zh.md)。

### `deploy`：创建、更新或恢复服务

安装平台后执行：

```bash
foretoken deploy examples/quickstart/qwen3-0.6b --timeout 20m
```

命令应用配置，并显示服务状态与容器日志，直到服务按配置就绪或停止。修改配置后重新执行，也可用它恢复已停止的部署。部署内容未变时，复用运行中的实例。

`--timeout` 设置 CLI 的等待时限，默认为十分钟；源码编译单独执行。各模型的准备和启动时间由 `spec.timeouts.startup` 控制。就绪失败或等待超时后，部署仍保留在 Kubernetes 中：用 `status` 查看原因，修正配置后重新部署。

### `endpoint`：获取访问入口

```bash
foretoken endpoint examples/quickstart/qwen3-0.6b
```

输出为前端 URL，可用于客户端或[发送测试请求](../README_zh.md#4-发送测试请求)。命令等待 LoadBalancer 或 Gateway 分配地址；服务就绪由 `deploy` 等待。

HTTP Gateway 模式下，用下面的命令获取请求 `Host` 头需要的域名：

```bash
foretoken endpoint examples/quickstart/qwen3-0.6b --host
```

直接访问时，`--host` 返回 URL 中的主机名或 IP，以及其中包含的端口。

### `stop`：暂时释放运行资源

```bash
foretoken stop examples/quickstart/qwen3-0.6b
```

命令等待前端和模型停止运行，释放 GPU，同时保留配置、存储和构建缓存。对同一目录执行 `foretoken deploy` 即可恢复，模型进程会重新启动并加载权重。

### `delete`：删除部署

```bash
foretoken delete examples/quickstart/qwen3-0.6b
```

命令删除目录中选择的资源，并清理不再使用的构建资源。配置包含 Namespace 时，也会删除该命名空间及其全部资源。缓存是否保留取决于[存储配置](../docs/features/model-storage_zh.md)。

## 3. 状态与日志

用 `foretoken status` 判断服务是否就绪、定位部署失败原因，或跟踪启动进度。命令只读取当前状态，不会应用配置。

### 查看一个部署

```bash
foretoken status examples/quickstart/qwen3-0.6b
```

输出展示各服务的状态和诊断信息，包括 Kubernetes 的等待与退出原因，以及容器重启前一次退出的诊断。

### 查看一个命名空间

```bash
foretoken status -n foretoken-demo
```

这会查看命名空间中的全部 Foretoken 模型服务和前端服务，包括由其他部署目录创建的服务。

### 持续跟踪状态和容器日志

```bash
foretoken status -n foretoken-demo --watch
```

`--watch` 持续显示状态变化和 Pod/容器日志，按 Ctrl+C 结束；也可与部署目录一起使用。结束查看后，服务继续运行。持久日志、指标与告警见[可观测性指南](../observability/README_zh.md)。

## 4. 评测与结果分析

用 `perf` 测响应速度和容量，用 `eval` 测模型质量，用 `plot` 分析已有结果。两种评测命令都接受 Kustomize 目录，也可以通过 `--url` 和 `--model` 指定已有的 OpenAI 兼容服务。部署包含多个模型时，用 `--model` 选择公开模型标识。

评测会复用已经部署的服务，测量配置或源码改动前先执行 `deploy`。部署不存在时，评测会基于已安装的平台临时启动服务，结束后回收本次创建的运行资源。

### `perf`：测量延迟和吞吐量

```bash
foretoken perf examples/quickstart/qwen3-0.6b --num-prompts 20 --output local
```

汇总结果包含请求成功情况、响应延迟和吞吐量，`--output local` 将结果保存下来，便于后续分析。提示词、数据集、并发数、请求到达速率和参数扫描见[性能评测示例](../benchmarks/docs/perf/README_zh.md)。

评测已有服务时，提供完整 API 地址、模型标识，以及提示词或数据集。将下面的 URL 和模型标识换成实际服务的值：

```bash
foretoken perf --url http://localhost:8000/v1/chat/completions \
  --model Qwen/Qwen3-0.6B --prompt Hello --num-prompts 20 --output local
```

### `eval`：为回答评分或比较模型

```bash
foretoken eval examples/quickstart/qwen3-0.6b \
  --evaluator lm-eval --tasks gsm8k --limit 100 --output local
```

该命令通过 lm-evaluation-harness 对 100 道 GSM8K 数学题评分，也可以选择 EvalScope；任务与评分参数沿用所选框架的语法。数据集和结果用法见[质量评测](../benchmarks/docs/eval/README_zh.md)。需要用 `--reference` 比较候选模型与参考模型的概率时，见[分布对比](../benchmarks/docs/eval/distribution-comparison_zh.md)。

### `plot`：分析已保存的结果

前面的示例会把每次运行的结果分别保存到 `results/` 下的独立目录。一起绘制这些已有结果：

```bash
foretoken plot results
```

图表保存在 `results/plots/`。需要选择特定运行时，传入 `perf` 或 `eval` 打印的一个或多个结果目录。评测时也可以直接使用 `--output plot` 导出图表。

需要 W&B 时，先执行 `wandb login`，并在开始评测时选择 `--output local,wandb`。已记录 W&B 输出的运行，包括离线运行，之后可用 `foretoken plot results --sync_wandb` 补传。更多选项见[绘图示例](../benchmarks/docs/recipes_zh.md#绘制与对照结果)和 [W&B 上传指南](../benchmarks/docs/perf/wandb_zh.md#恢复上传)。

## 5. 性能剖析

性能剖析记录模型的 CPU/GPU 执行时间线，用于定位瓶颈。通过 `--profile-engine` 选择 `pytorch`、NVIDIA `nsight` 或沐曦 `mctracer`，通过 `--profile-duration` 设置采集时长上限。部署需要持久化采集存储，快速开始示例已提供。各工具的环境准备见[性能剖析指南](../benchmarks/docs/profile/README_zh.md)。

### 采集评测负载

```bash
foretoken perf examples/quickstart/qwen3-0.6b \
  --profile --profile-engine pytorch --profile-duration 15s \
  --num-prompts 2 --max-tokens 128 --output local
```

`perf` 同时负责发送请求和协调采集，无需另行启动流量生成器。

### 采集外部流量

需要观察应用或其他流量生成器发来的请求时：

```bash
foretoken deploy examples/quickstart/qwen3-0.6b \
  --profile --profile-engine pytorch --profile-duration 15s --timeout 20m
```

命令部署服务并开启限时采集，在采集运行期间向服务发送请求即可。目录包含多个模型时，添加 `--model` 选择要采集的模型。

### `profile view`：查看采集结果

在使用浏览器的电脑上配置集群 kubeconfig，然后运行：

```bash
foretoken profile view
```

打开打印的本地 URL，即可查看当前与历史采集结果。使用 `-n foretoken-demo` 可只查看示例命名空间。按 Ctrl+C 关闭查看器，模型服务继续运行。
