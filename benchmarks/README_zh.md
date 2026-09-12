# 模型服务性能评测

[English](README.md) | 简体中文

使用 `foretoken bench` 评测模型服务性能。

被测服务需要提供 OpenAI-compatible Chat Completions API。评测已有模型服务时，请传入完整的 `/v1/chat/completions` URL。

## 安装

需要 Python 3.11 或更高版本。安装命令行工具和评测依赖：

```bash
pip install 'foretoken[bench]'

# 在源码仓库中安装：
# pip install -e '.[bench]'
```

## 完成第一次评测

示例会保存本地结果并上传到 Weights & Biases（W&B）。首次上传前运行 `wandb login`；仅需本地结果时使用 `--output local`。

选择 Foretoken 部署或已有服务 URL。

### Foretoken Kustomize 部署

在仓库根目录安装 Foretoken 平台，再评测[快速开始示例](../examples/quickstart/README_zh.md)：

```bash
foretoken install
foretoken bench examples/quickstart --number 10 --output local,wandb
```

模型服务已经运行时，命令会直接复用；尚未部署时，命令会应用 Kustomize 资源、等待服务就绪、完成评测，并且只清理本次创建的资源。Kustomize 评测未指定 `--prompt` 或 `--dataset` 时，默认发送 `Hello`。

部署只包含一个模型时，命令会自动确定模型名称；多模型部署需要添加 `--model MODEL_ID`。

### 已有模型服务

已有服务可以直接评测，不需要安装 Foretoken 平台或准备 Kubernetes：

```bash
foretoken bench \
  --url http://127.0.0.1:8008/v1/chat/completions \
  --model Qwen/Qwen3-0.6B \
  --prompt "你好" \
  --parallel 2 \
  --number 20 \
  --output local,wandb
```

使用 `--url` 时必须同时提供 `--model`，并且不能再传 Kustomize 路径。

## 选择负载

| 目标 | 负载来源 |
| --- | --- |
| 将固定提示词作为单轮对话重复发送 | `--prompt TEXT` |
| 使用本地对话数据 | `--dataset FILE.jsonl` |
| 使用 Hugging Face 数据集 | `--dataset ORG/NAME:SPLIT` |
| 使用 Hugging Face 数据集仓库中的文件 | `--dataset hf://datasets/ORG/NAME@REVISION/PATH` |
| 按 token 长度生成随机提示词 | `--dataset random --tokenizer-path TOKENIZER` |
| 按记录的到达时间回放请求 | `--trace TRACE --dataset DATASET` |
| 汇总多个数据集 | 在 `--dataset` 中用逗号分隔多个选择器 |
| 比较多组负载和生成参数 | 对 Kustomize 部署使用 `--sweep FILE.jsonl` |

每类负载的可复制命令见[评测配方](docs/examples_zh.md)。

## 对话如何执行

使用数据集评测时，每行是一段对话，默认执行全部用户轮次。用 `--max-turns N` 可以限制轮数。后续轮次使用模型之前的真实回答，而不是数据集中的参考答案。固定提示词和随机提示词都是单轮对话。

数据格式见[本地 JSONL 示例](docs/examples_zh.md#使用本地-jsonl-数据)和 [ShareGPT 示例](docs/examples_zh.md#执行-sharegpt-多轮对话)。

对话数据不支持工具定义、tool call 或 `tool` role 消息。实际包含多轮的对话不能使用正数 `--rate` 或 `--open-loop`。

## 控制请求负载

默认 `--rate -1`，在 `--parallel` 并发限制内尽快发送请求。默认不重试；`--max-retries N` 允许对暂时性故障最多额外尝试 `N` 次，重试耗时计入请求延迟。

- `--parallel N` 设置并发上限。
- `--number N` 设置对话数；固定提示词和随机提示词各生成单轮对话。
- `--rate R` 按每秒 `R` 个请求的泊松到达过程调度，同时保留并发上限。
- `--open-loop --rate R` 去掉并发上限，保持指定的请求到达率。

`--open-loop` 必须配合正数 `--rate`；不支持无并发上限且按最快速度发送的组合。

## 查找和阅读结果

启用本地结果时，命令会在评测结束后打印结果目录。结果默认位于 `results/<timestamp>/`，可用 `--output-dir PATH` 修改父目录。

先在控制台汇总或 `metrics.json` 中查看以下指标：

- **成功率**：比较性能前，先确认模型服务完成了预期负载。
- **Latency**：请求端到端耗时，p95 和 p99 反映尾部延迟。
- **TTFT**：流式响应从请求发出到首个 token 的时间。
- **TPOT**：流式响应产生首个 token 后，每个输出 token 的平均时间。
- **Generation tokens/s**：模型总输出吞吐量。
- **Generation tokens/s/user**：总输出吞吐量除以配置的并发数 `--parallel`；使用 `--open-loop` 时，该值等于总输出吞吐量。
- **Requests/s**：每秒成功完成的请求数。

使用 `--no-stream` 时，仍会统计请求延迟和吞吐量，但不报告 TTFT、TPOT 和 token 间隔。

参数扫描还会根据所选模型声明的 GPU 容量，报告每张 GPU 的生成吞吐量。

使用对话数据时，请求指标统计实际发送的 HTTP 轮次；对话部分提供对话级延迟和每秒尝试对话数。任一轮失败都会终止当前对话，因此成功轮次数不能解释为成功对话数。

`raw_output.json` 保存逐请求记录。标准负载还保留 `benchmark_data.db` 和 `benchmark.log`。比较不同配置的方法见[参数扫描](docs/examples_zh.md#扫描评测参数)。

## 选择结果去向

默认会打印汇总、保存本地结果，并尝试上传到 Weights & Biases（W&B）。W&B 不可用时，本地结果仍会保留。

| 目标 | 参数 |
| --- | --- |
| 仅保存本地结果 | `--output local` |
| 保存本地结果但不打印控制台汇总 | `--output local,quiet` |
| 保存本地结果并上传 W&B | 不传 `--output`，或使用 `--output local,wandb` |
| 仅上传 W&B | `--output wandb` |

使用 `--wandb-project`、`--wandb-entity` 和 `--wandb-run-name` 指定 W&B 项目、账号与运行名称。

完整参数见 `foretoken bench --help`。
