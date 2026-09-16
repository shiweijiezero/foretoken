# 模型服务性能评测

[English](README.md) | 简体中文

使用 `foretoken bench` 评测模型服务性能。

## 开始使用

需要 Python 3.11 或更高版本：

```bash
pip install 'foretoken[bench]'

# 如果使用源码安装：
# pip install -e '.[bench]'

wandb login
```

以下命令在仓库根目录执行。集群准备见[快速开始](../README_zh.md#快速开始)，已有平台可跳过安装：

```bash
foretoken install
foretoken bench examples/quickstart --number 10 --output local,wandb
```

默认发送 `Hello`。已部署的服务直接复用；临时部署的资源会在评测后清理。单模型部署自动选择模型，多模型时添加 `--model`。

## 常用命令

### 并发评测

```bash
foretoken bench examples/quickstart \
  --prompt "用一句话解释什么是 token。" \
  --parallel 8 --number 100 \
  --max-tokens 128 \
  --output local,wandb
```

`--parallel` 控制并发数，`--rate` 控制每秒请求到达率，各自设为 `-1` 表示不限。默认不限速、并发为 1。例如按平均每秒 5 个请求发送且不限并发：

```bash
foretoken bench examples/quickstart \
  --rate 5 --parallel -1 --number 100 \
  --output local,wandb
```

### 随机负载

```bash
foretoken bench examples/quickstart \
  --dataset random --tokenizer-path Qwen/Qwen3-0.6B \
  --min-prompt-length 128 --max-prompt-length 512 \
  --min-output-length 64 --max-output-length 256 \
  --parallel 8 --number 100 \
  --output local,wandb
```

输出长度控制需要服务支持 `min_tokens` 和 `ignore_eos`，未达到抽样长度的请求记为失败。不传输出上下界时，使用普通的 `--max-tokens` 上限，默认 4096。

### 数据集与多轮对话

```bash
foretoken bench examples/quickstart \
  --dataset r0b0tlab/qwen3.8-max-distillation-50k:train \
  --parallel 4 --number 20 \
  --output local,wandb
```

`--dataset` 也接受本地 JSONL 文件。每行是一段对话，默认运行全部轮次，并使用模型的真实回答继续；`--max-turns 1` 只运行首轮。多轮目前要求 `--rate -1`。

### 在评测时采集 Profile

对已部署的诊断服务，添加一次短时 PyTorch 采集：

```bash
foretoken bench examples/quickstart \
  --profile --profile-engine pytorch --profile-duration 15s \
  --number 2 --max-tokens 128 --output local
```

源码安装的平台需要支持[性能剖析](../observability/profiling_zh.md)，服务需要持久 RuntimeCache 存储。命令等待采集开始后才发送请求；负载完成后结束采集并等待导出。如果采集窗口先结束，负载仍跑完指定的请求数量。Profiling 会增加开销，正式性能测量应另跑一次不带 `--profile` 的评测。

此模式支持一个生成式负载，使用默认的 `--rate -1`；不支持仅提供 URL、轨迹回放、参数扫描或多个数据集。需要使用已有网络入口时，保留 Kustomize 路径并[添加 `--url`](#为部署指定请求入口)。`--wait-timeout` 分别限制启动与完成阶段的等待时长。本地 `profile.json` 记录这次运行及其 PVC 结果位置，trace 文件仍保存在 RuntimeCache。取消和查看结果见[性能剖析](../observability/profiling_zh.md)。

### 轨迹回放

```bash
foretoken bench examples/quickstart \
  --trace benchmarks/examples/trace.jsonl \
  --dataset benchmarks/examples/trace.jsonl \
  --trace-max-concurrency 4 --max-tokens 128 \
  --output local,wandb
```

轨迹记录决定请求数量和到达时间，每条记录独立回放。

### 参数扫描

```bash
foretoken bench examples/quickstart \
  --sweep benchmarks/examples/sweep.jsonl \
  --output local,wandb
```

参数扫描使用 Kustomize 部署，在同一模型服务上比较不同配置。

### 使用已有服务地址

对于默认模式下已部署的快速开始示例，先获取地址：

```bash
MODEL_SERVICE_URL="$(foretoken endpoint examples/quickstart)/v1/chat/completions"
foretoken bench \
  --url "$MODEL_SERVICE_URL" --model Qwen/Qwen3-0.6B \
  --prompt "你好" --number 20 \
  --output local,wandb
```

其他服务使用其实际 Chat Completions URL 和模型名称。不提供 Kustomize 路径时，命令不会访问 Kubernetes，且必须指定 `--model`。

### 为部署指定请求入口

如果前端已有可访问的 NodePort、代理或端口转发地址，可以保留 Kustomize 路径并添加 `--url`。地址必须指向同一部署。命令会跳过 LoadBalancer/Gateway 地址发现，继续检查部署就绪、选择模型、观察副本数，并保持原有资源清理方式。

例如，在一个终端中转发已部署的 Quick Start 前端：

```bash
kubectl port-forward --namespace foretoken-demo service/quickstart-frontend 8080:8080
```

然后在另一个终端运行：

```bash
foretoken bench examples/quickstart \
  --url http://127.0.0.1:8080/v1/chat/completions \
  --number 2 --max-tokens 128 --output local
```

`--url` 使用完整的 Chat Completions 地址。单模型部署仍自动选择模型，多模型需要 `--model`。使用 HTTP 地址且部署声明了 `FrontendService.spec.hostname` 时，CLI 会将该域名用作路由的 `Host` 请求头；HTTPS 地址需包含正确域名。添加[采集参数](#在评测时采集-profile)即可对同一已部署服务进行 profiling；`--profile` 仍要求 Kustomize 路径和已有部署。

## 查看结果

本地结果保存在 `results/` 下的独立目录，结束后会打印位置。`metrics.json` 是汇总，`raw_output.json` 是逐请求记录。

先看成功率、端到端耗时 E2EL 和输出 token 吞吐量。流式评测还报告首分片耗时 TTFT、平均输出 token 耗时 TPOT 和分片间隔 ITL；`--no-stream` 只关闭这些流式指标。

示例同时保存本地结果并上传 W&B。仅需本地结果用 `--output local`，修改结果父目录用 `--output-dir`。

各类用法见[常用命令](docs/examples_zh.md)，指标定义见[结果指标](metrics_zh.md)。全部参数见 `foretoken bench --help`。
