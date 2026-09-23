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
foretoken bench examples/quickstart --num-prompts 10 --output local,wandb
```

默认发送 `Hello`。已部署的服务直接复用；临时部署的资源会在评测后清理。单模型部署自动选择模型，多模型时添加 `--model`。

## 常用命令

### 并发评测

```bash
foretoken bench examples/quickstart \
  --prompt "用一句话解释什么是 token。" \
  --max-concurrency 8 --num-prompts 100 \
  --max-tokens 128 \
  --output local,wandb
```

添加 `--warmup-requests 16` 可在每次测量前完成 16 段预热对话，不计入正式指标。

`--max-concurrency` 控制在途请求数，`--request-rate` 控制每秒请求到达率。`--arrival-pattern` 可选 `constant`、`poisson` 或 `gamma`；`--burstiness` 控制 Gamma 到达的突发程度。`--duration` 到期后停止新的请求准入，并等待已经准入的请求完成；未显式传入 `--num-prompts` 时，评测时长就是请求预算，显式传入时两个条件同时生效。需要按时间戳回放时单独传入 `--trace`。预热请求使用当前负载控制，但不计入正式指标。`--request-rate -1` 表示取消速率限制，`--max-concurrency -1` 表示取消并发上限。默认不限速、并发为 1。例如按平均每秒 5 个请求发送且不限并发：

```bash
foretoken bench examples/quickstart \
  --request-rate 5 --max-concurrency -1 --num-prompts 100 \
  --output local,wandb
```

按时间限制运行时添加 `--duration 60`；达到 60 秒或请求预算后停止启动新请求。

### 随机负载

```bash
foretoken bench examples/quickstart \
  --dataset random --tokenizer-path Qwen/Qwen3-0.6B \
  --min-prompt-length 128 --max-prompt-length 512 \
  --min-output-length 64 --max-output-length 256 \
  --max-concurrency 8 --num-prompts 100 \
  --output local,wandb
```

输出长度控制需要服务支持 `min_tokens` 和 `ignore_eos`，未达到抽样长度的请求记为失败。不传输出上下界时，使用普通的 `--max-tokens` 上限，默认 4096。随机负载可以不传 `--num-prompts` 而只用 `--duration`，请求体会按需生成直到截止时间；两者同时传入时先达到的限制生效。

### 数据集与多轮对话

```bash
foretoken bench examples/quickstart \
  --dataset r0b0tlab/qwen3.8-max-distillation-50k:train \
  --max-concurrency 4 --num-prompts 20 \
  --output local,wandb
```

`--dataset` 也接受本地 JSONL 文件，以及逗号分隔的多个数据集。多个数据集共享同一个全局到达时钟和并发预算，结果保留数据集身份与分组指标。每行是一段对话，默认运行全部轮次。每轮都会请求模型生成，但后续请求默认使用数据集中的答案作为历史；添加 `--conversation-history generated` 可改用本次模型实际生成的回答。

`--num-prompts` 表示 HTTP 请求预算。多轮负载由 `--request-rate` 控制新对话的启动速率，依赖前序响应的后续轮次在响应完成后继续；`--max-concurrency` 限制同时进行的对话数。

### 在评测时采集 Profile

```bash
foretoken bench examples/quickstart \
  --profile --profile-engine pytorch --profile-duration 15s \
  --num-prompts 2 --max-tokens 128 --output local
```

Profile 会复用普通评测的负载控制；`--duration`、`--request-rate`、`--arrival-pattern` 和 `--max-concurrency` 仍然生效，采集在预热完成后开始，并在已准入请求排空后关闭。轨迹回放、多轮、多数据集、SLO 探测和 HTTP 参数扫描点都可使用同一个 Profile；扫描的每个参数点和重复运行都会在自己的目录写入 `profile.json`。Profile 仍只支持 Foretoken Kustomize 部署，不用于视频评测。环境配置和结果查看见[性能剖析](../observability/profiling_zh.md)。

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

参数扫描需传入部署配置目录，例如 `examples/quickstart`，目前不支持 `--url`。自定义负载点与配置对比见[参数扫描](docs/coomon_commands/sweep_zh.md)。

### SLO 并发搜索

```bash
foretoken bench examples/quickstart \
  --dataset random --tokenizer-path Qwen/Qwen3-0.6B \
  --min-prompt-length 128 --max-prompt-length 256 \
  --max-concurrency 2 \
  --slo-params '[{"p99_latency":"<=2"}]' \
  --slo-upper-bound 32 --output local,wandb
```

SLO 探测点保留生成式、多轮、多数据集和轨迹负载各自的调度语义。指标名与限制见 [SLO 并发搜索](docs/coomon_commands/slo_zh.md)。

### 使用已有服务地址

对于默认模式下已部署的快速开始示例，先获取地址：

```bash
MODEL_SERVICE_URL="$(foretoken endpoint examples/quickstart)/v1/chat/completions"
foretoken bench \
  --url "$MODEL_SERVICE_URL" --model Qwen/Qwen3-0.6B \
  --prompt "你好" --num-prompts 20 \
  --output local,wandb
```

其他服务使用其实际 Chat Completions URL 和模型名称。Gateway 模式传入上面的部署配置目录，由 CLI 配置路由请求头。

需要在请求前检查公开的健康端点时，添加 `--health-url https://model.example/health`；不传时，URL 模式直接开始评测。

## 查看结果

本地结果保存在 `results/` 下的独立目录，结束后会打印位置。`metrics.json` 是汇总，`raw_output.json` 是逐请求记录。

先看成功率、端到端耗时 E2EL 和输出 token 吞吐量。流式评测还报告首分片耗时 TTFT、平均输出 token 耗时 TPOT 和分片间隔 ITL；`--no-stream` 只关闭这些流式指标。

示例同时保存本地结果并上传 W&B。仅需本地结果用 `--output local`，修改结果父目录用 `--output-dir`。

各类用法见[常用命令](docs/examples_zh.md)，指标定义见[结果指标](metrics_zh.md)。全部参数见 `foretoken bench --help`。
