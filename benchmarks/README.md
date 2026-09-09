# Model Service Benchmarks

English | [简体中文](README_zh.md)

Use `foretoken bench` to measure the latency, throughput, and reliability of a model service. A benchmark can use either a Foretoken Kustomize deployment or the URL of an existing model service.

The service must provide an OpenAI-compatible Chat Completions API. Pass the full `/v1/chat/completions` URL when benchmarking an existing service.

## Install

Python 3.10 or later is required. Install the command and its benchmark dependencies:

```bash
pip install 'foretoken[bench]'

# From a source checkout:
# pip install -e '.[bench]'
```

## Run your first benchmark

Choose exactly one model service source.

### Foretoken Kustomize deployment

Install the Foretoken platform once, then benchmark a maintained deployment:

```bash
foretoken install
foretoken bench examples/quickstart --number 10 --output local
```

If the model service is already running, the command reuses it. Otherwise, it deploys the Kustomize resources, waits for the service, runs the benchmark, and removes only the resources it created. When no `--prompt` or `--dataset` is provided, a Kustomize benchmark sends `Hello`.

A deployment containing one model supplies the model name automatically. For a multi-model deployment, add `--model MODEL_ID`.

### Existing model service

Foretoken and Kubernetes are not required when the service is already available:

```bash
foretoken bench \
  --url http://127.0.0.1:8008/v1/chat/completions \
  --model Qwen/Qwen3-0.6B \
  --prompt "Hello" \
  --parallel 2 \
  --number 20 \
  --output local
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
| Compare workload and generation settings | `--bench-params FILE.jsonl` with a Kustomize deployment |

Copyable commands for each workload are in [Benchmark Recipes](docs/examples.md).

## How conversations run

Every non-trace workload is run as a conversation. A fixed prompt, random prompt, or dataset row with one `user` turn is a one-turn conversation. Local JSONL, Hugging Face, and ShareGPT rows may contain multiple turns.

- `--max-turns -1`, the default, runs every user turn in the row.
- `--max-turns N` runs only the first `N` user turns.
- `--number` counts conversations. For a dataset, each selected row is one conversation; `--parallel` limits concurrent conversations.
- An `assistant` message in the dataset is a reference boundary. Before the next user turn, its text is replaced by the model's actual answer.

Rows may use an OpenAI-style `messages` field, a `prompt` or `user` field, or ShareGPT `conversations` entries with `from: human|gpt` and `value`.

Conversation datasets do not support tool definitions, tool calls, or `tool` role messages. A multi-turn conversation cannot use a positive `--rate` or `--open-loop`.

## Control the request load

Without a positive `--rate`, requests are sent as fast as possible while `--parallel` limits concurrent work.

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
- **Generation tokens/s/user**: output throughput divided by `--parallel` for a closed-loop workload. It is a configured concurrency ratio, not a count of real users. Open-loop runs use a denominator of one.
- **Requests/s**: completed request throughput.

Parameter sweeps also report generation tokens per second per GPU, calculated from the GPU capacity declared for the selected model.

For conversation datasets, request metrics count the HTTP turns that actually ran. The conversation section reports conversation-level latency and attempted conversations per second. A failed turn stops that conversation, so successful turns are not the same as successful conversations.

`benchmark_data.db` contains request-level records for standard workloads, and `benchmark.log` contains failure details. Trace replay and parameter sweeps add workload-specific summaries in the same result directory. A sweep with at least two valid points also creates `pareto/PARETO.png`, comparing generation throughput per configured user or concurrent conversation with generation throughput per GPU.

## Select result destinations

By default, the command prints a summary, saves local results, and attempts to upload the run to Weights & Biases (W&B). If W&B is unavailable, local results remain available.

| Goal | Option |
| --- | --- |
| Local results only | `--output local` |
| Local results without console output | `--output local,quiet` |
| Local results and W&B | omit `--output`, or use `--output local,wandb` |
| W&B only | `--output wandb` |

Use `--wandb-project`, `--wandb-entity`, and `--wandb-run-name` to place and name W&B runs.

## Important combinations

- Trace replay treats every selected trace row as an independent request. It does not continue a conversation between records.
- A positive `--max-turns` cannot be combined with `--trace`. Use `--trace-max-concurrency` instead of `--parallel`; trace timestamps determine request count and arrival time.
- Mooncake prefix-reuse replay requires `--dataset random`, a tokenizer, and `--trace-synthetic-prefix-reuse`.
- Parameter sweeps require a Foretoken Kustomize deployment and cannot be combined with trace replay or multiple dataset selectors.
- `--no-stream` reports request latency but not TTFT or TPOT.

Run `foretoken bench --help` for the complete option reference.
