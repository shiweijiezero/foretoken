# Model Service Benchmarks

English | [简体中文](README_zh.md)

Use `foretoken bench` to measure model-service performance.

The service must provide an OpenAI-compatible Chat Completions API. Pass the full `/v1/chat/completions` URL when benchmarking an existing service.

## Install

Python 3.11 or later is required. Install the command and its benchmark dependencies:

```bash
pip install 'foretoken[bench]'

# From a source checkout:
# pip install -e '.[bench]'
```

## Run your first benchmark

The examples save results locally and upload them to Weights & Biases (W&B). Run `wandb login` before the first upload; use `--output local` if you only need local results.

Choose a Foretoken deployment or an existing service URL.

### Foretoken Kustomize deployment

From the repository root, install the Foretoken platform and benchmark the [Quick Start deployment](../examples/quickstart/README.md):

```bash
foretoken install
foretoken bench examples/quickstart --number 10 --output local,wandb
```

If the model service is already running, the command reuses it. Otherwise, it deploys the Kustomize resources, waits for the service, runs the benchmark, and removes only the resources it created. When no `--prompt` or `--dataset` is provided, a Kustomize benchmark sends `Hello`.

A deployment containing one model supplies the model name automatically. For a multi-model deployment, add `--model MODEL_ID`.

### Existing model service

An existing service needs only the benchmark client, not a Foretoken platform or Kubernetes cluster:

```bash
foretoken bench \
  --url http://127.0.0.1:8008/v1/chat/completions \
  --model Qwen/Qwen3-0.6B \
  --prompt "Hello" \
  --parallel 2 \
  --number 20 \
  --output local,wandb
```

`--url` requires `--model`. Do not pass a Kustomize path together with `--url`.

## Choose a workload

| Goal | Workload source |
| --- | --- |
| Repeat one prompt as one-turn conversations | `--prompt TEXT` |
| Use local conversations | `--dataset FILE.jsonl` |
| Use a Hugging Face dataset | `--dataset ORG/NAME:SPLIT` |
| Use a file from a Hugging Face dataset repository | `--dataset hf://datasets/ORG/NAME@REVISION/PATH` |
| Generate prompts with controlled token lengths | `--dataset random --tokenizer-path TOKENIZER` |
| Replay recorded arrival times | `--trace TRACE --dataset DATASET` |
| Combine datasets in one result | Comma-separate the `--dataset` selectors |
| Compare workload and generation settings | `--sweep FILE.jsonl` with a Kustomize deployment |

Copyable commands for each workload are in [Benchmark Recipes](docs/examples.md).

## How conversations run

For dataset workloads, each row is one conversation. All user turns run by default; use `--max-turns N` to limit them. Later turns use the model's actual previous answers rather than the dataset's reference answers. Fixed and random prompts are single-turn conversations.

See the [local JSONL](docs/examples.md#use-a-local-jsonl-dataset) and [ShareGPT](docs/examples.md#run-sharegpt-conversations) examples for supported data formats.

Conversation datasets do not support tool definitions, tool calls, or `tool` role messages. A multi-turn conversation cannot use a positive `--rate` or `--open-loop`.

## Control the request load

The default `--rate -1` sends requests as fast as possible within the `--parallel` limit. Requests are not retried by default; `--max-retries N` allows up to `N` additional attempts for transient failures, included in request latency.

- `--parallel N` sets the concurrency limit.
- `--number N` sets the conversation count; fixed and random prompts produce one-turn conversations.
- `--rate R` schedules Poisson arrivals at `R` requests per second while retaining the concurrency limit.
- `--open-loop --rate R` removes the concurrency limit and preserves the requested arrival rate.

Use a positive `--rate` with `--open-loop`; an unlimited as-fast-as-possible workload is not supported.

## Find and read results

When local output is enabled, the command prints the result directory after the run. Results are stored under `results/<timestamp>/` by default. Use `--output-dir PATH` to choose another parent directory.

Start with these values in the console summary or `metrics.json`:

- **Success rate**: confirm the model service completed the intended workload before comparing performance.
- **Latency**: end-to-end request time; use p95 or p99 to understand tail behavior.
- **TTFT**: time to first token for streamed responses.
- **TPOT**: time per output token after the first token for streamed responses.
- **Generation tokens/s**: total output throughput.
- **Generation tokens/s/user**: output throughput divided by the configured concurrency, `--parallel`. With `--open-loop`, this equals total output throughput.
- **Requests/s**: successfully completed requests per second.

With `--no-stream`, request latency and throughput remain available, but TTFT, TPOT, and inter-token latency are not reported.

Parameter sweeps also report generation tokens per second per GPU, calculated from the GPU capacity declared for the selected model.

For conversation datasets, request metrics count the HTTP turns that actually ran. The conversation section reports conversation-level latency and attempted conversations per second. A failed turn stops that conversation, so successful turns are not the same as successful conversations.

`raw_output.json` contains per-request records. Standard workloads also retain `benchmark_data.db` and `benchmark.log`. See [parameter sweeps](docs/examples.md#sweep-benchmark-parameters) to compare configurations.

## Select result destinations

By default, the command prints a summary, saves local results, and attempts to upload the run to Weights & Biases (W&B). If W&B is unavailable, local results remain available.

| Goal | Option |
| --- | --- |
| Local results only | `--output local` |
| Local results without console output | `--output local,quiet` |
| Local results and W&B | omit `--output`, or use `--output local,wandb` |
| W&B only | `--output wandb` |

Use `--wandb-project`, `--wandb-entity`, and `--wandb-run-name` to place and name W&B runs.

Run `foretoken bench --help` for all options.
