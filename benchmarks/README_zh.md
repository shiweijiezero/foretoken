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

添加 `--warmup-requests 16` 可在每次正式运行前完成独立预热。预热和测量分段显示，各自提供按已完成对话计数的进度条。预热全部成功后才开始测量；预热结果保存在 `warmup/` 下，不计入正式指标。

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

```bash
foretoken bench examples/quickstart \
  --profile --profile-engine pytorch --profile-duration 15s \
  --number 2 --max-tokens 128 --output local
```

环境配置和结果查看见[性能剖析](../observability/profiling_zh.md)。

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
  --dataset random --tokenizer-path Qwen/Qwen3-0.6B \
  --min-prompt-length 128 --max-prompt-length 256 --random-seed 0 \
  --sweep benchmarks/examples/sweep.jsonl \
  --warmup-requests 16 --num-runs 3 \
  --output local,wandb
```

参数扫描在同一 Kustomize 部署上改变负载，不与 `--url` 组合。比较精度、量化或推测解码时，对每种部署配置复用相同的扫描文件，步骤见[参数扫描](docs/coomon_commands/sweep_zh.md)。

### 使用已有服务地址

对于默认模式下已部署的快速开始示例，先获取地址：

```bash
MODEL_SERVICE_URL="$(foretoken endpoint examples/quickstart)/v1/chat/completions"
foretoken bench \
  --url "$MODEL_SERVICE_URL" --model Qwen/Qwen3-0.6B \
  --prompt "你好" --number 20 \
  --output local,wandb
```

其他服务使用其实际 Chat Completions URL 和模型名称。Gateway 模式使用上面的 Kustomize 写法，由 CLI 配置路由请求头。

## 查看结果

本地结果保存在 `results/` 下的独立目录，结束后会打印位置。`metrics.json` 是汇总，`raw_output.json` 是逐请求记录。

`environment.json` 记录客户端软件信息；Kustomize 模式还记录运行前后的服务设置。具体内容见[实验记录](metrics_zh.md#实验记录)。

扫描将全部重复运行保存在 `sweep_points.json`，并将各参数点的统计结果写入 `sweep_summary.json` 和 `sweep_summary.csv`。使用这些汇总比较重复结果，避免只挑最快的一次。

先看成功率、端到端耗时 E2EL 和输出 token 吞吐量。流式评测还报告首分片耗时 TTFT、平均输出 token 耗时 TPOT 和分片间隔 ITL；`--no-stream` 只关闭这些流式指标。

示例同时保存本地结果并上传 W&B。仅需本地结果用 `--output local`，修改结果父目录用 `--output-dir`。

各类用法见[常用命令](docs/examples_zh.md)，指标定义见[结果指标](metrics_zh.md)。全部参数见 `foretoken bench --help`。
