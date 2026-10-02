# Parameter sweeps

English | [简体中文](sweep_zh.md) · [Performance examples](README.md)

After [setup](README.md#setup), compare concurrency settings with the existing [parameter file](../../examples/sweep.jsonl):

```bash
foretoken perf examples/quickstart \
  --dataset random --min-prompt-length 128 --max-prompt-length 256 \
  --temperature 0 --sweep benchmarks/examples/sweep.jsonl \
  --warmup-requests 16 --num-runs 3 \
  --experiment-name concurrency --output local,wandb,plot
```

This measures concurrency 1, 2, and 4 with a fixed 256-token output target. Each point is repeated three times, with 16 warmup conversations before each repetition.

## Choose a workload

Common parameter files are maintained in [`scripts/common/`](../../scripts/common/). Pass a file with `--sweep` to measure its parameter combinations:

| File | Purpose |
| --- | --- |
| [`fixed-length.jsonl`](../../scripts/common/fixed-length.jsonl) | Compare concurrency with balanced, long-input, and long-output workloads |
| [`fixed-arrival.jsonl`](../../scripts/common/fixed-arrival.jsonl) | Compare request rates with short fixed inputs and outputs |
| [`fixed-capacity.jsonl`](../../scripts/common/fixed-capacity.jsonl) | Compare concurrency with long fixed inputs and short outputs |
| [`long-context.jsonl`](../../scripts/common/long-context.jsonl) | Vary input length; set output length and concurrency in the command |
| [`conversation-rate.jsonl`](../../scripts/common/conversation-rate.jsonl) | Vary conversation start rates for a selected dataset |
| [`studychat-conversation.jsonl`](../../scripts/common/studychat-conversation.jsonl) | Vary concurrency for a StudyChat workload |
| [`slo-thresholds.jsonl`](../../scripts/common/slo-thresholds.jsonl) | Compare request-level SLO thresholds at different conversation start rates |
| [`quantized-models.jsonl`](../../scripts/common/quantized-models.jsonl) | Compare BF16 and 4-bit deployments across concurrency settings |

For fixed-length workloads:

```bash
foretoken perf examples/quickstart --dataset random \
  --sweep benchmarks/scripts/common/fixed-length.jsonl --num-runs 1 --num-prompts 32 \
  --warmup-requests 4 --temperature 0 --output local,wandb,plot
```

Keep only length pairs and concurrency levels supported by the selected model and service. The model context must accommodate input, output, and chat-template overhead. Fixed output lengths require service support for `min_tokens` and `ignore_eos`. The tokenizer is inferred from the selected model; use `--tokenizer-path` to override it.

Each JSONL row keeps related settings together. Fields use CLI names with underscores, such as `request_rate`. Lists define sweep axes, and multiple axes in a row expand into all combinations. To mix two datasets in each run, use a nested list: `"dataset": [["first.jsonl", "second.jsonl"]]`.

`--slo-params` scores the fixed workload. Add `--slo-search` to [search concurrency](slo.md); with a sweep, `--num-runs` repeats the complete search for each point, with one measurement per probe.

## Compare SLO thresholds and request rates

The [SLO threshold configuration](../../scripts/common/slo-thresholds.jsonl) varies request-level TTFT limits and conversation start rates, keeping the TPOT limit fixed. Replace the URL and model with your service's Chat Completions endpoint and model:

```bash
foretoken perf --url http://host/v1/chat/completions --model Qwen/Qwen3-0.6B \
  --dataset hf://datasets/anon8231489123/ShareGPT_Vicuna_unfiltered/ShareGPT_V3_unfiltered_cleaned_split.json \
  --sweep benchmarks/scripts/common/slo-thresholds.jsonl \
  --temperature 0 --random-seed 0 --max-concurrency -1 \
  --num-prompts 100 --warmup-requests 0 --num-runs 1 \
  --experiment-name slo-thresholds --output local,wandb,plot
```

Each request must meet both timing limits. The 100-request budget counts individual HTTP turns. Compare attainment and goodput at each rate; threshold curves hold the comparison operator and other workload settings fixed, while rate curves show a separate slice for each threshold. See [fixed-rate SLO measurement](slo.md#measure-attainment-at-fixed-conversation-rates) for interpreting attainment.

In a JSONL row, `"slo_params": [{"ttft": "<=250ms", "tpot": "<=100ms"}, {"ttft": "<=500ms", "tpot": "<=100ms"}]` scans two criteria choices. With `--slo-search`, nest objects to run several independent searches within one choice: `"slo_params": [[{"p99_ttft": "<=250ms"}, {"p99_tpot": "<=100ms"}]]`.

## Measure longer input contexts

Use the [long-context configuration](../../scripts/common/long-context.jsonl) with one concurrent request and a fixed 512-token output target:

```bash
foretoken perf examples/quickstart --dataset random \
  --sweep benchmarks/scripts/common/long-context.jsonl \
  --min-output-length 512 --max-output-length 512 --max-concurrency 1 \
  --num-prompts 4 --warmup-requests 1 --num-runs 1 \
  --experiment-name long-context --output local,wandb,plot
```

Keep only rows your model can serve: its context must fit the input, 512 output tokens, and chat-template overhead. Random input lengths are generation targets; check reported input-token usage for actual lengths. These small samples help compare input-length sensitivity; use a larger request budget and repeated measurements to assess tail latency or SLO capacity.

## Run conversation workloads

For a StudyChat concurrency comparison:

```bash
foretoken perf examples/quickstart \
  --dataset hf://datasets/KrisQ/StudyChat/data.jsonl \
  --sweep benchmarks/scripts/common/studychat-conversation.jsonl \
  --max-tokens 128 --temperature 0 --num-runs 3 \
  --warmup-requests 20 --num-prompts 1000 --output local,wandb,plot
```

To compare conversation start rates on ShareGPT and StudyChat, use the same [rate configuration](../../scripts/common/conversation-rate.jsonl) with each dataset:

```bash
foretoken perf examples/quickstart \
  --dataset hf://datasets/anon8231489123/ShareGPT_Vicuna_unfiltered/ShareGPT_V3_unfiltered_cleaned_split.json \
  --sweep benchmarks/scripts/common/conversation-rate.jsonl \
  --temperature 0 --random-seed 0 --max-concurrency -1 \
  --num-prompts 100 --warmup-requests 0 --num-runs 1 \
  --experiment-name sharegpt-rate --output local,wandb,plot

foretoken perf examples/quickstart \
  --dataset hf://datasets/KrisQ/StudyChat/data.jsonl \
  --sweep benchmarks/scripts/common/conversation-rate.jsonl \
  --temperature 0 --random-seed 0 --max-concurrency -1 \
  --num-prompts 100 --warmup-requests 0 --num-runs 1 \
  --experiment-name studychat-rate --output local,wandb,plot
```

The rate controls conversation starts, while the request budget counts HTTP turns; the last conversation may stop when the budget is reached. Turns with text reference answers generate matching token counts, and turns without one use the `--max-tokens` limit. See [conversation output lengths](conversations.md) for overrides. For recorded arrival times, use [Mooncake trace replay](mooncake-trace.md).

## Compare methods

Pass multiple Kustomize examples to measure the same workload on each service:

```bash
foretoken perf examples/quickstart examples/quickstart3 \
  --dataset random --num-prompts 100 --output local,wandb,plot
```

Multiple endpoints use one `--url` followed by several URLs; `--model` accepts one shared model or one model per URL. A sweep row's `service` list provides the same choices. The [quantized-model sweep](../../scripts/common/quantized-models.jsonl) compares the [BF16 and 4-bit deployments](../../../examples/quantized-model/README.md):

```bash
foretoken perf --dataset random --sweep benchmarks/scripts/common/quantized-models.jsonl \
  --num-prompts 100 --warmup-requests 10 --num-runs 3 \
  --temperature 0 --experiment-name methods --output local,wandb,plot
```

Service paths are relative to the repository root. An endpoint choice uses `name`, `url`, and `model`; `health_url` is optional. Authentication uses `--api-key`.

All points for one method run before the next method starts. Temporary deployments are removed between methods; existing services are reused unchanged. Apply configuration changes to an existing service with `foretoken deploy` before measuring them.

## Read results and redraw

Use `sweep_summary.csv` and the comparison plots to compare latency and throughput under the same workload settings. Error bars show sample standard deviation across repetitions, including for per-run percentiles; one repetition has no error estimate. Check each metric's sample count and failed runs when comparing points.

Reusing the same `--experiment-name` replaces that experiment directory; omitting it creates a timestamped directory. Redraw the first example at double-column width without sending requests:

```bash
foretoken plot results/concurrency --columns 2
```

Use `--metric` to select a summary metric and `--method` to select a named method; both may be repeated. `--output-dir` saves an alternative layout separately. See [output settings](../../README.md#read-and-save-results) for destination selection.

## Video settings

```bash
foretoken perf video \
  --url http://127.0.0.1:8091/v1/videos/sync \
  --dataset VideoArgusBench/TI2V \
  --sweep benchmarks/examples/video-sweep.jsonl \
  --num-runs 2 --output local,wandb,plot
```

Each point retains generated videos and performance metrics. See [video workloads](video.md) for generation settings and service requirements.
