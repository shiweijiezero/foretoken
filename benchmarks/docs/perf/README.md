# Measure serving performance

English | [简体中文](README_zh.md) · [Evaluation and profiling](../../README.md)

Use `foretoken perf` to compare response latency, token generation speed, and throughput under different workloads.

## Setup

Complete the [shared setup](../../README.md#get-started), then run commands from the repository root. The basic examples use `examples/quickstart/qwen3-0.6b`.

For Foretoken deployments, use the Kustomize directory so the CLI resolves the service URL, model and Gateway routing headers automatically.

For another OpenAI-compatible API, replace the directory with `--url "$MODEL_SERVICE_URL" --model "$MODEL_ID"`. Set the variables to your service's actual Chat Completions URL and public model ID, for example:

```bash
export MODEL_SERVICE_URL=http://localhost:8000/v1/chat/completions
export MODEL_ID=Qwen/Qwen3-0.6B
```

## Commands

| Task | Examples |
| --- | --- |
| Run a simple workload | [Fixed prompts](fixed-prompt.md), [non-streaming requests](non-streaming.md) |
| Control input and output lengths | [Random workloads](random.md) |
| Replay real conversations | [Local conversations](conversations.md), [Hugging Face datasets](huggingface.md), [ShareGPT](sharegpt.md), [tool data](tools.md) |
| Mix datasets or control arrivals | [Multiple datasets](multi-dataset.md), [arrival rate and concurrency](arrival-rate.md) |
| Replay recorded traffic | [StudyChat](studychat.md), [Mooncake traces](mooncake-trace.md) |
| Replay agent sessions | [AgentX](agentx.md) |
| Compare load settings | [Parameter sweeps](sweep.md), [SLO concurrency search](slo.md) |
| Measure video generation | [Video workloads](video.md) |
| Compare runs and inspect charts | [W&B output](wandb.md) |

Metric definitions are in [Performance metrics](../metrics.md). List options with `foretoken perf --help`, or `foretoken perf --video --help` for video generation. To investigate an execution bottleneck, [capture a profile](../profile/README.md) alongside a workload.
