# Measure serving performance

English | [简体中文](README_zh.md) · [Evaluation and profiling](../../README.md)

Use `foretoken perf` to compare response latency, token generation speed, and throughput under different workloads.

## Setup

Complete the [shared setup](../../README.md#get-started), then run commands from the repository root. The examples use `examples/quickstart`.

For an existing endpoint, replace that directory with `--url "$MODEL_SERVICE_URL" --model "$MODEL_ID"`. For the deployed Quick Start, obtain those values with:

```bash
MODEL_SERVICE_BASE_URL="$(foretoken endpoint examples/quickstart)"
export MODEL_SERVICE_URL="${MODEL_SERVICE_BASE_URL%/}/v1/chat/completions"
export MODEL_ID=Qwen/Qwen3-0.6B
```

Use the actual Chat Completions URL and model ID for other services. Gateway access and HTTP parameter sweeps use the Kustomize directory; Foretoken discovers the Gateway routing headers from it.

## Commands

| Task | Examples |
| --- | --- |
| Run a simple workload | [Fixed prompts](fixed-prompt.md), [non-streaming requests](non-streaming.md) |
| Control input and output lengths | [Random workloads](random.md) |
| Replay real conversations | [Local conversations](conversations.md), [Hugging Face datasets](huggingface.md), [ShareGPT](sharegpt.md), [tool data](tools.md) |
| Mix datasets or control arrivals | [Multiple datasets](multi-dataset.md), [arrival rate and concurrency](arrival-rate.md) |
| Replay recorded traffic | [StudyChat](studychat.md), [Mooncake traces](mooncake-trace.md) |
| Compare load settings | [Parameter sweeps](sweep.md), [SLO concurrency search](slo.md) |
| Measure video generation | [Video workloads](video.md) |
| Compare runs and inspect charts | [W&B output](wandb.md) |

Metric definitions are in [Performance metrics](../../metrics.md). All options are listed by `foretoken perf --help`. To investigate an execution bottleneck, [capture a profile](../profile/README.md) alongside a workload.
