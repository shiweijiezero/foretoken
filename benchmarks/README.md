# Model Service Benchmarks

English | [简体中文](README_zh.md)

Use `foretoken bench` to measure model-service performance.

## Get started

Python 3.11 or later is required:

```bash
pip install 'foretoken[bench]'

# From a source checkout:
# pip install -e '.[bench]'

wandb login
```

Run the following commands from the repository root. Follow the [Quick Start](../README.md#quick-start) to prepare the cluster; skip installation if the platform is already installed:

```bash
foretoken install
foretoken bench examples/quickstart --number 10 --output local,wandb
```

The default prompt is `Hello`. Existing services are reused; resources deployed temporarily for the benchmark are removed afterwards. A single-model deployment supplies the model name automatically. Add `--model` for a multi-model deployment.

## Common commands

### Concurrent requests

```bash
foretoken bench examples/quickstart \
  --prompt "Explain what a token is in one sentence." \
  --parallel 8 --number 100 \
  --max-tokens 128 \
  --output local,wandb
```

`--parallel` controls concurrency and `--rate` controls arrivals per second. Each accepts `-1` for no limit. The defaults are no rate limit and one concurrent request. To send at an average of five requests per second without a concurrency cap:

```bash
foretoken bench examples/quickstart \
  --rate 5 --parallel -1 --number 100 \
  --output local,wandb
```

### Random workloads

```bash
foretoken bench examples/quickstart \
  --dataset random --tokenizer-path Qwen/Qwen3-0.6B \
  --min-prompt-length 128 --max-prompt-length 512 \
  --min-output-length 64 --max-output-length 256 \
  --parallel 8 --number 100 \
  --output local,wandb
```

Output-length control requires service support for `min_tokens` and `ignore_eos`. Requests that miss the sampled length count as failures. Without these output bounds, generation uses the ordinary `--max-tokens` limit, which defaults to 4096.

### Datasets and conversations

```bash
foretoken bench examples/quickstart \
  --dataset r0b0tlab/qwen3.8-max-distillation-50k:train \
  --parallel 4 --number 20 \
  --output local,wandb
```

`--dataset` also accepts a local JSONL file. Each row is a conversation, and all turns run by default using the model's actual answers. Use `--max-turns 1` for the first turn only. Multi-turn conversations currently require `--rate -1`.

### Trace replay

```bash
foretoken bench examples/quickstart \
  --trace benchmarks/examples/trace.jsonl \
  --dataset benchmarks/examples/trace.jsonl \
  --trace-max-concurrency 4 --max-tokens 128 \
  --output local,wandb
```

The trace determines request count and arrival times. Each record is replayed independently.

### Parameter sweeps

```bash
foretoken bench examples/quickstart \
  --sweep benchmarks/examples/sweep.jsonl \
  --output local,wandb
```

Sweeps use a Kustomize deployment to compare configurations against the same model service.

### SLA auto-tune

```bash
foretoken bench examples/quickstart \
  --dataset random --tokenizer-path Qwen/Qwen3-0.6B \
  --min-prompt-length 128 --max-prompt-length 256 \
  --parallel 2 \
  --sla-params '[{"p99_latency":"<=2"}]' \
  --sla-upper-bound 32 --output local,wandb
```

Search reuses EvalScope; see [SLA auto-tune](docs/coomon_commands/sla.md) for metric names and limits.

### An existing service URL

For the Quick Start already deployed in the default mode, resolve its address first:

```bash
MODEL_SERVICE_URL="$(foretoken endpoint examples/quickstart)/v1/chat/completions"
foretoken bench \
  --url "$MODEL_SERVICE_URL" --model Qwen/Qwen3-0.6B \
  --prompt "Hello" --number 20 \
  --output local,wandb
```

For another service, use its actual Chat Completions URL and model name. In Gateway mode, use the Kustomize form above so the CLI supplies routing headers.

## Read results

Local results are saved in a separate directory under `results/`, printed when the run finishes. `metrics.json` contains the summary and `raw_output.json` contains per-request records.

Start with success rate, end-to-end latency (E2EL), and output token throughput. Streamed runs also report time to the first chunk (TTFT), average time per output token (TPOT), and inter-chunk intervals (ITL). `--no-stream` disables only these streaming metrics.

The examples save results locally and upload them to W&B. Use `--output local` for local results only, and `--output-dir` to change the parent directory.

See [Common commands](docs/examples.md) for individual guides and examples, or [Result metrics](metrics.md) for metric definitions. Run `foretoken bench --help` for all options.
