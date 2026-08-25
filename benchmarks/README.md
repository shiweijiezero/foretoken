# Benchmarks

English | [简体中文](README_zh.md)

`benchmarks/` is the evaluation module for Foretoken.

It can discover a deployed Foretoken service from its Kustomize configuration or connect to an existing OpenAI-compatible endpoint, then measure performance, compare configurations, and check whether answer quality meets the bar. The goal is reproducible experiments that answer: can this service hold latency and throughput, and is the quality good enough?

## When to Use It

- You want latency and throughput at a given concurrency or arrival rate.
- You want to compare concurrency, request count, generation settings, or server configs.
- You want to confirm the model is not only fast, but also correct on answers and tool use.
- You want a suitable load point or capacity plan under latency and throughput targets.

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

The console summary is shown by default. Use `--output local` for local artifacts, `--output wandb` for W&B, or a comma-separated combination. Add `quiet` to suppress console output. Local files are written under `--output-dir`.

## Examples

Benchmark a Foretoken Kubernetes deployment. The CLI reuses it when already present; otherwise it deploys the rendered resources and removes only those resources after the benchmark. When neither `--prompt` nor `--dataset` is specified, it uses a short built-in prompt:

```bash
foretoken bench --deploy examples/quickstart
```

Use the common sampling options directly and pass other OpenAI-compatible or backend-specific request fields through `--extra-body`:

```bash
foretoken bench --deploy examples/quickstart \
  --temperature 0 \
  --top-p 1 \
  --top-k 0 \
  --extra-body '{"seed":7,"min_tokens":8}'
```

Benchmark an existing endpoint with a fixed prompt:

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
  --output local,wandb
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
  --output local,wandb
```

HuggingFace dataset id (rows: `messages`, `prompt`, or `user`[+`system`]):

```bash
foretoken bench \
  --url http://127.0.0.1:8008/v1/chat/completions \
  --model Qwen3.6-27B \
  --dataset r0b0tlab/qwen3.8-max-distillation-50k:train \
  --parallel 4 \
  --number 20 \
  --output local,wandb
```

Multiple JSONL / HuggingFace sources can be comma-separated. `--number` is the total request count across all sources. Requests are divided in source order; when the total is not divisible evenly, each earlier source receives one extra request. Sources run sequentially, then raw results are merged and metrics recomputed. When `wandb` is selected,
the experiment is one W&B **group** and each dataset is its own **run**:

```bash
foretoken bench \
  --url http://127.0.0.1:8008/v1/chat/completions \
  --model Qwen3.6-27B \
  --dataset /path/a.jsonl,org/name:train,/path/b.jsonl \
  --parallel 4 \
  --number 30 \
  --output local,wandb
```
