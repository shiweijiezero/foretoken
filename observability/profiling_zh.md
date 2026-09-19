<!--
SPDX-License-Identifier: Apache-2.0
SPDX-FileCopyrightText: Copyright contributors to the Foretoken project
-->

# 性能剖析

[English](profiling.md) | 简体中文

使用 PyTorch Profiler 或 NVIDIA Nsight Systems 查看模型推理的执行时间线。目前支持 NVIDIA GPU 上的 vLLM，需使用[源码安装](../docs/custom-deployment_zh.md)的 CLI 和平台。采集结果使用持久 RuntimeCache 保存，快速开始示例已配置好该存储。

## 同时运行 benchmark 和采集

从仓库根目录执行：

```bash
pip install -e '.[bench]'
foretoken bench examples/quickstart \
  --profile --profile-engine pytorch --profile-duration 15s \
  --number 2 --max-tokens 128 --output local
```

此模式支持 Kustomize 部署中的单个生成式负载，使用默认的 `--rate -1`。

## 部署并采集外部流量

```bash
foretoken deploy examples/quickstart \
  --profile --profile-engine pytorch --profile-duration 15s
```

服务就绪后开始采集，请求由外部发送；采集结束后服务继续运行。多模型部署用 `--model MODEL_ID` 选择采集对象。`--profile-duration` 设置最长记录时间，评测负载提前结束时也会停止采集。

## Nsight Systems

Nsight Systems 采集 CUDA 和 NVTX 时间线。在模型启动前通过 `ModelService.spec.profiling.engine: nsight` 选择该工具；更改工具会替换模型进程。省略此字段时准备 PyTorch。采集命令的 `--profile-engine` 必须与已准备的工具一致。

### 准备诊断镜像

完成源码安装后，用本机构建的 model-server 镜像制作 Linux x86_64 诊断镜像。将 `NSIGHT_IMAGE` 设为有推送权限、且集群可以拉取的镜像地址：

```bash
docker build -f deploy/inference-engines/nsight/Dockerfile \
  --build-arg MODEL_SERVER_IMAGE=foretoken-dev-model-server \
  -t "$NSIGHT_IMAGE" deploy/inference-engines/nsight
docker push "$NSIGHT_IMAGE"
```

将以下配置保存为 `nsight-values.yaml`，并用该镜像地址替换 `YOUR_NSIGHT_IMAGE`：

```yaml
runtime:
  vllm:
    nsightImage: YOUR_NSIGHT_IMAGE
```

在此集群原有的源码安装命令后加上 `--values nsight-values.yaml` 应用配置。只有选择 Nsight 的模型使用诊断镜像。

### 采集

[Nsight 示例](../examples/nsight/README_zh.md)已选择该工具，并沿用快速开始示例的持久存储：

```bash
foretoken bench examples/nsight \
  --profile --profile-engine nsight --profile-duration 15s \
  --number 2 --max-tokens 128 --output local
```

如需采集外部流量，改用 `foretoken deploy examples/nsight --profile --profile-engine nsight --profile-duration 15s --timeout 20m`。该命令在采集后保留运行中的服务，再次执行即可采集下一段。

## 查看结果

在本地电脑使用目标集群的 kubeconfig 执行：

```bash
foretoken profile view
```

打开打印的网址，浏览采集目录及子目录。PyTorch trace 在 Perfetto 中查看，浏览器需能访问 `ui.perfetto.dev`。Nsight 报告可下载为 `.nsys-rep` 文件并用 Nsight Systems 打开，也可下载 SQLite 导出文件进行分析。按 Ctrl+C 关闭查看器，文件会保留。

不再需要该部署及采集记录时清理：

```bash
foretoken delete examples/quickstart
```

清理 Nsight 示例时将路径换为 `examples/nsight`。Profiling 会增加开销，延迟和吞吐量对比请使用不带 `--profile` 的评测。
