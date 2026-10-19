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

使用 Python 3.11 或更高版本：

```bash
pip install foretoken
foretoken --version
```

部署和本地集群示例使用仓库中的配置文件。获取仓库后，从仓库根目录执行这些示例：

```bash
git clone https://github.com/shiweijiezero/foretoken.git
cd foretoken
```

开发 CLI 时，在该目录执行 `pip install -e .`。使用 uv 时，通过 `uv venv` 和 `source .venv/bin/activate` 创建并激活虚拟环境，再执行 `uv pip install foretoken`。

安装平台和部署服务需要 `kubectl`、Helm 4.2.4 或更高版本，以及目标集群的 kubeconfig。评测已有 URL、绘制本地结果时无需 Kubernetes。

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

安装或更新 Foretoken 共享平台，准备模型部署所需的基础组件、监控和持久日志。具体模型通过 `foretoken deploy` 部署。

```bash
# 安装发布版平台
foretoken install

# 从当前 Foretoken 仓库源码构建
foretoken install -e .
```

集群需提供默认 StorageClass，GPU 节点需具备厂商驱动和设备插件。新安装默认通过 `LoadBalancer` 直接访问；更新后的平台默认配置在服务重新部署后生效。

用 `--frontend-mode gateway` 启用[网关访问](../README_zh.md#网关模式)。复用已有 Gateway 时，同时指定 `--gateway-name` 和 `--gateway-namespace`；需要选择监听器时，再添加 `--gateway-section-name`。

`--values` 应用 [Helm 配置](../deploy/charts/foretoken/values.yaml)，`--oci-registry` 指定默认镜像和 Chart 的仓库前缀。监控设置见[可观测性](../observability/README_zh.md#平台设置)，节点间模型分发见[模型存储](../docs/features/model-storage_zh.md#在节点间分发模型)。

源码构建时，标准 kind、k3d context 可直接载入镜像，其他集群需指定 `--registry`；仓库设置与代码更新见[源码部署](../docs/deployment/source_zh.md)。

### uninstall

删除模型部署后，卸载共享平台。Kubernetes 集群、Foretoken CRD、已保存的日志和复用的集群组件会保留。

```bash
foretoken uninstall
```

## 服务部署

这些命令接收包含 `kustomization.yaml` 及其引用资源的 Kustomize 目录。示例使用[单模型快速开始](../examples/quickstart/qwen3-0.6b/README_zh.md)，资源需求和配置说明见该示例。

### deploy

创建、更新或恢复目录中定义的模型与前端服务，并等待服务达到配置要求的状态。

```bash
foretoken deploy examples/quickstart/qwen3-0.6b --timeout 20m
```

修改配置后重新执行；源码安装也用同一命令部署代码改动。`--timeout` 设置 CLI 等待时间，不改变模型自身的启动时限。等待失败后，服务仍保留在集群中，可通过 `status` 查看原因。

### stop

停止服务、释放 GPU，保留配置、存储和构建缓存。对同一目录执行 `deploy` 即可恢复。

```bash
foretoken stop examples/quickstart/qwen3-0.6b
```

### delete

删除部署目录中选择的资源，并清理不再使用的构建资源。配置包含 Namespace 时，也会删除该命名空间及其全部资源。

```bash
foretoken delete examples/quickstart/qwen3-0.6b
```

缓存是否保留取决于[存储配置](../docs/features/model-storage_zh.md)。

### endpoint

输出供 API 客户端使用的前端 URL。`--host` 输出请求的 `Host` 头取值，在 HTTP Gateway 模式下为路由域名。

```bash
# 公开访问 URL
foretoken endpoint examples/quickstart/qwen3-0.6b

# HTTP Host 取值
foretoken endpoint examples/quickstart/qwen3-0.6b --host
```

该命令等待访问地址分配，服务就绪由 `deploy` 等待。客户端用法见[发送请求](../README_zh.md#4-发送测试请求)。

## 状态与日志

### status

查看服务是否就绪、启动进度，以及 Pod/容器的失败原因。可按部署目录或命名空间选择范围：

```bash
# 查看一个部署的服务
foretoken status examples/quickstart/qwen3-0.6b

# 查看命名空间中的全部模型与前端服务
foretoken status -n foretoken-demo

# 持续跟踪状态变化和容器日志
foretoken status -n foretoken-demo --watch
```

`--watch` 也可用于部署目录。按 Ctrl+C 结束查看，服务继续运行。持久日志、指标和告警见[可观测性指南](../observability/README_zh.md)。

## 评测与结果分析

`perf` 和 `eval` 接受部署目录或已有 API 地址。部署包含多个模型时，用 `--model` 指定公开模型标识。

评测复用已有服务，测量配置或源码改动前先执行 `deploy`。部署不存在时，会基于已安装的平台临时启动服务，并在评测结束后回收本次创建的运行资源。

### perf

在指定负载下测量响应延迟、请求吞吐量和 token 吞吐量。

```bash
# 评测 Foretoken 部署
foretoken perf examples/quickstart/qwen3-0.6b --num-prompts 20 --output local

# 评测已有 API；按实际服务替换 URL 和模型标识
foretoken perf --url http://localhost:8000/v1/chat/completions \
  --model Qwen/Qwen3-0.6B --prompt Hello --num-prompts 20 --output local
```

URL 方式需要完整请求地址，并显式提供提示词或数据集。`--output local` 将每次结果保存到 `results/` 下，无需 W&B 账号。数据集、并发数、请求到达速率和参数扫描见[性能评测](../benchmarks/docs/perf/README_zh.md)。

### eval

通过 lm-evaluation-harness 或 EvalScope 为模型回答评分。下面评测 100 道 GSM8K 数学题：

```bash
foretoken eval examples/quickstart/qwen3-0.6b \
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
foretoken deploy examples/quickstart/qwen3-0.6b \
  --profile --profile-engine pytorch --profile-duration 15s --timeout 20m

# 发送评测请求并采集执行过程
foretoken perf examples/quickstart/qwen3-0.6b \
  --profile --profile-engine pytorch --profile-duration 15s \
  --num-prompts 2 --max-tokens 128 --output local
```

采集需要 Foretoken 部署目录，并同时指定 `--profile-engine` 和 `--profile-duration`。支持 `pytorch`、NVIDIA `nsight` 和沐曦 `mctracer`。快速开始示例已提供持久化采集存储，各工具的准备见[性能剖析指南](../benchmarks/docs/profile/README_zh.md)。

### profile view

浏览保存在集群中的采集结果。在使用浏览器的电脑上配置目标集群的 kubeconfig，然后运行：

```bash
foretoken profile view
```

打开打印的本地 URL。添加 `-n foretoken-demo` 可只查看一个命名空间。按 Ctrl+C 关闭查看器，模型服务和采集结果会保留。
