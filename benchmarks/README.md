# Benchmarks

English | [简体中文](README_zh.md)

`benchmarks/` is the evaluation module for Foretoken.

It sends requests to a deployed inference service, measures performance, compares configurations, and checks whether answer quality meets the bar. The goal is reproducible experiments that answer: can this service hold latency and throughput, and is the quality good enough?

## When to Use It

- You want latency and throughput at a given concurrency or arrival rate.
- You want to compare concurrency, request count, generation settings, or server configs.
- You want to confirm the model is not only fast, but also correct on answers and tool use.
- You want a suitable load point or capacity plan under latency and throughput targets.

If you are only poking the API by hand, you usually do not need the full evaluation flow.

## Main Features

| Feature | Description |
|---|---|
| Performance benchmark | Stress the inference service and measure latency, throughput, time to first token, and related metrics |
| Load sweep | Sweep concurrency, request count, or arrival rate to see how performance changes |
| Parameter sweep | Combine server and bench parameters to compare configurations in batch |
| Correctness evaluation | Check answer quality and tool calling, not speed alone |
| SLO evaluation | Search or simulate against latency and quality targets to guide capacity and autoscaling |

## What It Produces

- Readable summary results in the console
- Locally saved configs, raw results, and metrics for later review
- Optional Weights & Biases (W&B) experiment logs and charts for cross-run comparison and config selection

## Examples

Fixed prompt:

```bash
foretoken bench \
  --url http://127.0.0.1:8008/v1/chat/completions \
  --model Qwen3.6-27B \
  --prompt "hello" \
  --parallel 2 \
  --number 20
```

Local dataset file:

```bash
foretoken bench \
  --url http://127.0.0.1:8008/v1/chat/completions \
  --model Qwen3.6-27B \
  --dataset foretoken/conversation.jsonl \
  --parallel 4 \
  --number 20 \
  --wandb
```

Random synthetic prompts (tokenizer required):

```bash
foretoken bench \
  --url http://127.0.0.1:8008/v1/chat/completions \
  --model Qwen3.6-27B \
  --dataset random \
  --tokenizer-path Qwen/Qwen3.6-27B \
  --min-prompt-length 128 --max-prompt-length 512 \
  --parallel 4 --number 20 --max-tokens 64 \
  --rate 5 \
  --wandb
```

HuggingFace dataset id (rows: `messages`, `prompt`, or `user`[+`system`]):

```bash
foretoken bench \
  --url http://127.0.0.1:8008/v1/chat/completions \
  --model Qwen3.6-27B \
  --dataset r0b0tlab/qwen3.8-max-distillation-50k:train \
  --parallel 4 \
  --number 20 \
  --wandb
```

Multiple JSONL / HuggingFace sources (comma-separated). `--number` is the
**total** across all datasets (split evenly); each source runs sequentially,
then raw results are merged and metrics recomputed. With `--wandb`, the
experiment is one W&B **group** and each dataset is its own **run**:

```bash
foretoken bench \
  --url http://127.0.0.1:8008/v1/chat/completions \
  --model Qwen3.6-27B \
  --dataset /path/a.jsonl,org/name:train,/path/b.jsonl \
  --parallel 4 \
  --number 30 \
  --wandb
```
