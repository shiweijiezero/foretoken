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
  --number 200
```

Dataset file:

```bash
foretoken bench \
  --url http://127.0.0.1:8008/v1/chat/completions \
  --model Qwen3.6-27B \
  --dataset-path /home/wshiah/code/zhuting/foretoken/conversation.jsonl \
  --parallel 2 \
  --number 200
```
