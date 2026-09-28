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

Common experiment files are maintained in [`scripts/common/`](../../scripts/common/). The [fixed-length sweep](../../scripts/common/fixed-length.jsonl) keeps three input/output length pairs in separate JSONL rows and expands each row's concurrency list.

```bash
foretoken perf examples/quickstart --dataset random \
  --sweep benchmarks/scripts/common/fixed-length.jsonl --num-runs 3 --num-prompts 1000 \
  --warmup-requests 20 --temperature 0 --output local,wandb,plot
```

This produces nine parameter points and 27 measured runs. Concurrency 1 supplies the single-request comparison; latency, throughput, and resource plots reuse those runs. The tokenizer comes from the selected model service; `--tokenizer-path` overrides it.

Load, generation, and dataset options use their CLI names with underscores. For example, `request_rate: [4, 8, 16]` scans arrival rates. The [fixed-arrival configuration](../../scripts/common/fixed-arrival.jsonl) and [capacity configuration](../../scripts/common/fixed-capacity.jsonl) are ready-to-run examples. Lists are sweep axes: to mix two datasets in each run, use `"dataset": [["first.jsonl", "second.jsonl"]]`. SLO criteria are supplied with `--slo-params`; `--num-runs` repeats the complete search for each point, with one measurement per probe.

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

The result directory contains `sweep_summary.csv`, individual run directories, and `plots/` with PDF, SVG, PNG, and CSV exports. Statistics retain each metric's sample count; error bars show the sample standard deviation across runs, including for per-run percentiles. A single repetition has no error estimate.

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
