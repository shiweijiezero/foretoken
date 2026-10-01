# Parameter sweeps

English | [简体中文](sweep_zh.md) · [Performance examples](README.md)

Compare load settings and export figures with the existing [parameter file](../../examples/sweep.jsonl):

```bash
foretoken perf examples/quickstart \
  --dataset random --min-prompt-length 128 --max-prompt-length 256 \
  --temperature 0 --sweep benchmarks/examples/sweep.jsonl \
  --warmup-requests 16 --num-runs 3 \
  --experiment-name concurrency --output local,wandb,plot
```

This runs 384 requests at concurrency 1, 2 and 4, requesting 256 output tokens each. Every point is repeated three times, with 16 warmup conversations before each repetition. A sweep also accepts `--url` with `--model`, and supports the same conversation, mixed-dataset, trace, and profile options as a single run.

## Keep related parameters together

Common experiment files are maintained in [`scripts/common/`](../../scripts/common/). The [fixed-length sweep](../../scripts/common/fixed-length.jsonl) keeps five input/output length pairs in separate JSONL rows and expands each row's concurrency list.

```bash
foretoken perf examples/quickstart --dataset random \
  --sweep benchmarks/scripts/common/fixed-length.jsonl --num-runs 1 --num-prompts 32 \
  --warmup-requests 4 --temperature 0 --output local,wandb,plot
```

The five input/output pairs are 8,192/2,048, 32,768/4,096, 131,072/4,096, 8,192/16,384, and 32,768/16,384 tokens. At concurrency 1, 8, 16, and 32, they produce 20 parameter points, each measured once, with 32 measured requests and 4 warmup requests per repetition. Use only pairs and concurrency levels supported by the selected model and service; the model context must accommodate both input and output. Concurrency 1 supplies the single-request comparison; latency, throughput, and resource plots reuse those runs. The tokenizer comes from the selected model service; `--tokenizer-path` overrides it.

Load, generation, and dataset options use their CLI names with underscores. For example, `request_rate: [4, 8, 16]` scans arrival rates. The [fixed-arrival configuration](../../scripts/common/fixed-arrival.jsonl) and [capacity configuration](../../scripts/common/fixed-capacity.jsonl) are ready-to-run examples. Lists are sweep axes: to mix two datasets in each run, use `"dataset": [["first.jsonl", "second.jsonl"]]`. Add `--slo-search` to search concurrency instead of scoring a fixed load; then `--num-runs` repeats the complete search for each point, with one measurement per probe.

## Compare SLO thresholds and request rates

The [SLO threshold configuration](../../scripts/common/slo-thresholds.jsonl) scans conversation start rates and request-level TTFT limits. Replace the URL and model with your service's Chat Completions endpoint and model:

```bash
foretoken perf --url http://host/v1/chat/completions --model Qwen/Qwen3-0.6B \
  --dataset hf://datasets/anon8231489123/ShareGPT_Vicuna_unfiltered/ShareGPT_V3_unfiltered_cleaned_split.json \
  --sweep benchmarks/scripts/common/slo-thresholds.jsonl \
  --temperature 0 --random-seed 0 --max-concurrency -1 \
  --num-prompts 100 --warmup-requests 0 --num-runs 1 \
  --experiment-name slo-thresholds --output local,wandb,plot
```

The 12 points vary TTFT limits of 125, 250, and 500 ms across conversation start rates of 2, 4, 8, and 16 per second, holding TPOT at 100 ms. The 100-request budget counts individual HTTP turns. This is a Foretoken threshold-sensitivity workload, not a paper protocol. Local and W&B results compare attainment and goodput; a single run has no repeat error estimate. In a JSONL row, `"slo_params": [{"ttft": "<=250ms", "tpot": "<=100ms"}, {"ttft": "<=500ms", "tpot": "<=100ms"}]` scans two request-level criteria, each requiring both conditions. With `--slo-search`, nest the objects only when one choice contains several independent searches: `"slo_params": [[{"p99_ttft": "<=250ms"}, {"p99_tpot": "<=100ms"}]]`. The threshold plots keep the comparison operator and other workload settings fixed while varying their numeric x-axis; request-rate plots use a separate slice for each threshold.

## Measure longer input contexts

Use the [long-context configuration](../../scripts/common/long-context.jsonl) to compare input-length sensitivity at one concurrent request and a fixed 512-token output target:

```bash
foretoken perf examples/quickstart --dataset random \
  --sweep benchmarks/scripts/common/long-context.jsonl \
  --min-output-length 512 --max-output-length 512 --max-concurrency 1 \
  --num-prompts 4 --warmup-requests 1 --num-runs 1 \
  --experiment-name long-context --output local,wandb,plot
```

The six rows request input lengths of 16,384; 32,768; 65,536; 131,072; 262,144; and 512,000 tokens. Each point runs four measured requests and one warmup. Keep only rows your model can serve: its supported context must fit the input, 512 output tokens, and any chat-template overhead. Random input lengths are generation targets; check the reported input-token usage for the actual lengths. This is an input-length sensitivity workload, not a p99/SLO capacity measurement.

## Data-driven workloads

Run a real dataset workload with the maintained [StudyChat concurrency configuration](../../scripts/common/studychat-conversation.jsonl):

```bash
foretoken perf examples/quickstart \
  --dataset hf://datasets/KrisQ/StudyChat/data.jsonl \
  --sweep benchmarks/scripts/common/studychat-conversation.jsonl \
  --max-tokens 128 --temperature 0 --num-runs 3 \
  --warmup-requests 20 --num-prompts 1000 --output local,wandb,plot
```

This scans concurrency 1, 8, 16, and 32: four points and 12 measured runs.

To compare conversation arrival rates on ShareGPT and StudyChat, use the same [rate configuration](../../scripts/common/conversation-rate.jsonl) with each dataset:

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

Each dataset scans 2, 4, 8, and 16 conversation starts per second: four points and four measured runs. The 100-request budget counts HTTP turns, so the last conversation may stop when the budget is reached. Turns with text reference answers automatically generate matching token counts; turns without one use the `--max-tokens` limit. See [conversation output lengths](conversations.md) for explicit overrides.

Replay the [Mooncake Conversation trace](mooncake-trace.md) directly, without a sweep file. Dataset runs preserve task rows and length distributions; trace replay preserves recorded arrival times, output targets, and shared-prefix metadata.

## Compare methods

Multiple Kustomize examples can also be passed directly for a single workload:

```bash
foretoken perf examples/quickstart examples/quickstart3 \
  --dataset random --num-prompts 100 --output local,wandb,plot
```

Multiple endpoints use one `--url` followed by several URLs; `--model` accepts one shared model or one model per URL. A row's `service` list provides the same choices inside a reusable sweep file. Kustomize examples can be written directly as paths; the [quantized-model sweep](../../scripts/common/quantized-models.jsonl) compares the [BF16 and 4-bit deployments](../../../examples/quantized-model/README.md):

```bash
foretoken perf --dataset random --sweep benchmarks/scripts/common/quantized-models.jsonl \
  --num-prompts 100 --warmup-requests 10 --num-runs 3 \
  --temperature 0 --experiment-name methods --output local,wandb,plot
```

Service paths are relative to the repository root. An endpoint choice uses `name`, `url`, and `model`; `health_url` is optional. Authentication uses the command's `--api-key`.

All points for one method run before the next method starts. Temporary deployments are removed between methods; existing services are reused unchanged. To measure a changed configuration on an existing service, apply it with `foretoken deploy` first.

## Read results and redraw

The result directory contains `sweep_summary.csv`, individual run directories, and `plots/` with PDF, SVG, PNG, and CSV exports. For Kustomize runs with Prometheus, speculative decoding acceptance and stage-time estimates also appear in the sweep summary and comparison plots when available. Statistics retain each metric's sample count; error bars show the sample standard deviation across runs, including for per-run percentiles. A single repetition has no error estimate.

W&B groups the individual runs and adds a comparison run with the same summary data and curves. With `plot` selected, it also receives the exported figures and tables. Reusing the same `--experiment-name` replaces that experiment directory; omitting it creates a timestamped directory.

Redraw the first example at double-column width without sending requests:

```bash
foretoken plot results/concurrency --columns 2
```

Use `--metric` to select a summary metric and `--method` to select a named method; both may be repeated. `--output-dir` writes an alternative layout to a separate directory. See [output settings](../../README.md#read-and-save-results) for destination selection.

## Video settings

```bash
foretoken perf video \
  --url http://127.0.0.1:8091/v1/videos/sync \
  --dataset VideoArgusBench/TI2V \
  --sweep benchmarks/examples/video-sweep.jsonl \
  --num-runs 2 --output local,wandb,plot
```

Video points may vary `width`, `height`, `num_frames`, `fps`, `num_inference_steps`, `aspect_ratio`, `flow_shift`, `audio_flow_shift`, `seed`, `max_concurrency`, `duration`, and `warmup_requests`. Each point retains its generated videos and performance metrics.
