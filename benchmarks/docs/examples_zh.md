# 评测示例

[English](examples.md) | 简体中文

## 数据集选择器

`--dataset` 支持本地 JSONL、Hugging Face 数据集，以及数据集仓库中的文件：

```text
/path/to/conversation.jsonl
org/dataset:train
hf://datasets/org/dataset@main/path/to/conversation.jsonl
```

多个来源可用逗号分隔。Hub 文件 URI 必须包含 `datasets` 仓库类型。

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
  --parallel 4 --max-tokens 64 \
  --rate 5
```

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

数据源按顺序运行，随后合并结果。`--number` 由全部数据源共享并按顺序分配；不能整除时，前面的数据源各多一个请求。

```bash
foretoken bench \
  --url http://127.0.0.1:8008/v1/chat/completions \
  --model Qwen/Qwen3-0.6B \
  --dataset r0b0tlab/qwen3.8-max-distillation-50k:train,ianncity/GLM-5.2-Conversation:train \
  --parallel 4 \
  --number 20
```

## SLA 并发搜索

通过二分查找算法，自动寻找满足 SLA 约束的最大并发数

```bash
foretoken bench \
  --url http://127.0.0.1:8008/v1/chat/completions \
  --model Qwen/Qwen3-0.6B \
  --dataset random \
  --tokenizer-path Qwen/Qwen3-0.6B \
  --min-prompt-length 128 --max-prompt-length 512 \
  --parallel 4 --max-tokens 64 \
  --sla-auto-tune \
  --sla-params '[{"avg_ttft": "<=0.2", "avg_tpot": "<=0.025"}]' \
  --sla-upper-bound 32 \
  --sla-number-multiplier 5
```

`--sla-params` 为 JSON 数组，支持 AND、OR，以及二者混用：

- AND（同一对象内多条件）：`--sla-params '[{"avg_ttft":"<=0.2","avg_tpot":"<=0.025"}]'`
- OR（数组内多个对象）：`--sla-params '[{"p99_ttft":"<0.05"},{"p99_ttft":"<0.01"}]'`
- AND 与 OR 混用：`--sla-params '[{"avg_ttft":"<=0.2","avg_tpot":"<=0.025"},{"rps":">=10"}]'`

约束条件使用 `<`、`<=`、`>` 或 `>=`，阈值须为有限数值。延迟阈值单位为秒。支持指标：`avg_latency`、`p99_latency`、`p95_latency`、`avg_ttft`、`p99_ttft`、`p95_ttft`、`p50_ttft`、`avg_tpot`、`p99_tpot`、`p95_tpot`、`p50_tpot`、`rps`、`tps`。TTFT/TPOT 要求流式响应（默认开启）。

搜索从 `--parallel` 开始，在 `--sla-lower-bound` 与 `--sla-upper-bound` 之间进行（默认 1 和 65536）。每个并发按 `--num-runs` 重复（默认 1），对各轮指标取均值。每轮请求数为 `round(parallel × --sla-number-multiplier)`（默认倍数 2），忽略 `--number`。使用 `--dataset random` 时，数据偏移逐轮递增，以避免重复提示词命中 KV 缓存；其他数据集保持配置的数据起点。

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
