# Benchmark Examples

English | [简体中文](examples_zh.md)

## Dataset selectors

`--dataset` accepts local JSONL, Hugging Face datasets, and files in dataset repositories:

```text
/path/to/conversation.jsonl
org/dataset:train
hf://datasets/org/dataset@main/path/to/conversation.jsonl
```

Multiple sources may be comma-separated. A Hub file URI must include the `datasets` repository type.

## Random prompts

Random prompts require a tokenizer:

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

## Hugging Face and local datasets

```bash
# Hugging Face dataset
foretoken bench \
  --url http://127.0.0.1:8008/v1/chat/completions \
  --model Qwen/Qwen3-0.6B \
  --dataset r0b0tlab/qwen3.8-max-distillation-50k:train \
  --parallel 4 \
  --number 20

# Local JSONL dataset
foretoken bench \
  --url http://127.0.0.1:8008/v1/chat/completions \
  --model Qwen/Qwen3-0.6B \
  --dataset /path/to/conversation.jsonl \
  --parallel 4 \
  --number 20
```

## Trace replay

`--trace` supplies arrival timestamps and `--dataset` supplies request content. The benchmark detects StudyChat and Mooncake trace formats. `--trace-start` and `--trace-duration` select `[first + start, first + start + duration)`; waiting for `--trace-max-concurrency` counts toward replay delay.

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

Replay a Mooncake trace with random payloads and shared synthetic prefixes:

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

## Multiple datasets

Sources run in order and their results are merged. `--number` is shared across the sources and divided in source order; earlier sources receive one extra request when division is uneven.

```bash
foretoken bench \
  --url http://127.0.0.1:8008/v1/chat/completions \
  --model Qwen/Qwen3-0.6B \
  --dataset r0b0tlab/qwen3.8-max-distillation-50k:train,ianncity/GLM-5.2-Conversation:train \
  --parallel 4 \
  --number 20
```

## Parameter sweep

`--bench-params` accepts a JSONL file. Each line overrides request execution fields; list-valued `parallel`, `number`, and `rate` expand into separate points. A `rate` of `-1` sends requests as fast as possible.

`benchmarks/examples/bench_params.jsonl` contains a maintained example:

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

Every valid point is saved. A sweep with at least two valid points also writes `pareto/PARETO.png`.
