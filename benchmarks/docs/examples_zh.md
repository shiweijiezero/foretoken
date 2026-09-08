# HTTP 性能评测示例

[English](examples.md) | 简体中文

## 数据集选择器

`--dataset` 支持本地 JSONL、Hugging Face 数据集，以及数据集仓库中的文件：

```text
/path/to/conversation.jsonl
org/dataset:train
hf://datasets/org/dataset@main/path/to/conversation.jsonl
```

多个数据集选择器可用逗号分隔。Hub 文件 URI 必须包含 `datasets` 仓库类型。

## 随机提示词

随机提示词需要 tokenizer：

```bash
foretoken bench \
  --url http://127.0.0.1:8008/v1/chat/completions \
  --model Qwen/Qwen3-0.6B \
  --dataset random \
  --tokenizer-path Qwen/Qwen3-0.6B \
  --random-seed 0 \
  --min-prompt-length 128 --max-prompt-length 512 \
  --parallel 4 --number 20 --max-tokens 64 \
  --rate 5
```

配置的长度用于生成合成 token 序列，序列随后会被解码成文本。文本再次分词后长度可能略有变化，因此请求规模以服务端返回的输入 token 指标为准。

## Hugging Face 与本地数据集

```bash
# Hugging Face 数据集
foretoken bench \
  --url http://127.0.0.1:8008/v1/chat/completions \
  --model Qwen/Qwen3-0.6B \
  --dataset r0b0tlab/qwen3.8-max-distillation-50k:train \
  --parallel 4 \
  --number 20

# 本地 JSONL 数据集
foretoken bench \
  --url http://127.0.0.1:8008/v1/chat/completions \
  --model Qwen/Qwen3-0.6B \
  --dataset /path/to/conversation.jsonl \
  --parallel 4 \
  --number 20
```

## 交互式多轮对话

当数据集的每一行表示一段交互式对话时，可用 `--max-turns N` 限制用户轮数；该参数是启用多轮模式的唯一开关。使用 `--max-turns -1` 执行数据集定义的完整对话。此时 `--number` 表示对话数，`--parallel` 表示同时推进的对话数。Foretoken 不会根据通用 `messages` 或 ShareGPT 行结构猜测执行模式，因为同一段历史也可能表示一个独立请求。

```bash
foretoken bench \
  --url http://127.0.0.1:8008/v1/chat/completions \
  --model Qwen/Qwen3-0.6B \
  --dataset kth8/multi-turn-conversation-50000x:train \
  --max-turns 4 \
  --parallel 2 --number 10
```

本地 JSONL 和 Hugging Face 数据可以使用 OpenAI 风格的 `messages` 字段。本地 JSONL 也接受直接写消息数组：

```jsonl
{"messages":[{"role":"system","content":"请简短回答。"},{"role":"user","content":"说出一种三原色。"},{"role":"assistant","content":"红色。"},{"role":"user","content":"再说一种。"},{"role":"assistant","content":"蓝色。"}]}
```

已经核验的 ShareGPT `conversations` 格式也可使用，其中 `from` 为 `human` 或 `gpt`，正文位于 `value`。数据集中的 assistant 回答只用于标记轮次边界：EvalScope 会丢弃其内容，加入模型的真实回答，再发送下一轮用户消息。最后一个用户消息后的 reference assistant 只负责结束该轮，不会回传给模型。

system 消息和图片 content 数组等结构化 Chat Completions 内容会继续保留在消息历史中。普通对话 runner 不执行工具循环，因此多轮模式会拒绝工具定义、tool call 和 `tool` role 消息。多轮模式当前不能与轨迹回放、open-loop 或正数 `--rate` 组合。

组合多个多轮数据集时，`--number` 仍按选择器顺序分配；兼容字段 `dataset_request_counts` 在该模式下记录分配到各数据集的对话数。顶层结果精确合并 HTTP 轮次指标、耗时和尝试对话吞吐量；每个数据集的对话分布保留在 `conversation.per_dataset`。参数扫描复用同一多轮 runner，bench-params 行中的 `max_turns` 也会为该负载点启用多轮模式。

## 轨迹回放

`--trace` 提供到达时间，`--dataset` 提供请求内容。评测会识别 StudyChat 和 Mooncake 轨迹格式。`--trace-start` 与 `--trace-duration` 选择 `[first + start, first + start + duration)`；等待 `--trace-max-concurrency` 的时间计入回放延迟。

```bash
foretoken bench \
  --url http://127.0.0.1:8008/v1/chat/completions \
  --model Qwen/Qwen3-0.6B \
  --trace KrisQ/StudyChat \
  --dataset KrisQ/StudyChat \
  --trace-start 600 \
  --trace-duration 300 \
  --trace-max-concurrency 32
```

使用随机请求内容和合成共享前缀回放 Mooncake 轨迹：

```bash
foretoken bench \
  --url http://127.0.0.1:8008/v1/chat/completions \
  --model Qwen/Qwen3-0.6B \
  --trace valeriol29/mooncake-traces \
  --trace-start 2620 \
  --trace-duration 30 \
  --dataset random \
  --tokenizer-path Qwen/Qwen3-0.6B \
  --random-seed 0 \
  --trace-synthetic-prefix-reuse \
  --trace-max-concurrency 16 \
  --max-tokens 64
```

## 多数据集

数据集按选择器顺序运行，随后合并结果。`--number` 由全部数据集共享并按顺序分配；不能整除时，前面的数据集各多一个请求。

```bash
foretoken bench \
  --url http://127.0.0.1:8008/v1/chat/completions \
  --model Qwen/Qwen3-0.6B \
  --dataset r0b0tlab/qwen3.8-max-distillation-50k:train,ianncity/GLM-5.2-Conversation:train \
  --parallel 4 \
  --number 20
```

## 参数扫描

`--bench-params` 接收 JSONL 文件。每行覆盖请求执行字段；`parallel`、`number` 和 `rate` 的列表值会展开为独立负载点。`rate: -1` 表示按最快速度发送请求。

`benchmarks/examples/bench_params.jsonl` 提供维护中的示例：

```jsonl
{"_benchmark_name": "n10", "parallel": [1, 2, 4, 8], "number": 10, "max_tokens": 64}
{"_benchmark_name": "n20", "parallel": [1, 2], "number": 20, "max_tokens": 128}
```

```bash
foretoken bench examples/quickstart \
  --dataset random \
  --tokenizer-path Qwen/Qwen3-0.6B \
  --min-prompt-length 128 --max-prompt-length 512 \
  --bench-params benchmarks/examples/bench_params.jsonl
```

评测会保存每个有效负载点。至少有两个有效负载点时，还会写入 `pareto/PARETO.png`。
