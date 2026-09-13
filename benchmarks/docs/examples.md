# Common benchmark commands

English | [简体中文](examples_zh.md)

## Setup

Install the benchmark client as described in [Model Service Benchmarks](../README.md). Run commands from the repository root, using the cluster prepared by the [Quick Start](../../README.md#quick-start). Run `wandb login` before first using W&B.

The command guides use `examples/quickstart`. For another existing service, replace that path with `--url "$MODEL_SERVICE_URL" --model "$MODEL_ID"`. For the Quick Start already deployed in the default mode, obtain those values with:

```bash
MODEL_SERVICE_BASE_URL="$(foretoken endpoint examples/quickstart)"
export MODEL_SERVICE_URL="${MODEL_SERVICE_BASE_URL%/}/v1/chat/completions"
export MODEL_ID=Qwen/Qwen3-0.6B
```

Use the actual Chat Completions URL and model ID for other services. In Gateway mode, use the Kustomize path so Foretoken supplies routing headers. Parameter sweeps require the Kustomize form.

## Commands

- [Fixed prompts](coomon_commands/fixed-prompt.md)
- [Non-streaming requests](coomon_commands/non-streaming.md)
- [Random workloads](coomon_commands/random.md)
- [Local conversations](coomon_commands/conversations.md)
- [Hugging Face datasets](coomon_commands/huggingface.md)
- [ShareGPT conversations](coomon_commands/sharegpt.md)
- [Tool data](coomon_commands/tools.md)
- [Multiple datasets](coomon_commands/multi-dataset.md)
- [Arrival rate and concurrency](coomon_commands/arrival-rate.md)
- [StudyChat replay](coomon_commands/studychat.md)
- [Mooncake prefix reuse](coomon_commands/mooncake.md)
- [Parameter sweeps](coomon_commands/sweep.md)
- [W&B output](coomon_commands/wandb.md)

Metric definitions are in [Result metrics](coomon_commands/metrics.md). All options are listed by `foretoken bench --help`.
