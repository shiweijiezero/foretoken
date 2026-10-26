<!--
SPDX-License-Identifier: Apache-2.0
SPDX-FileCopyrightText: Copyright contributors to the Foretoken project
-->

# Plot saved benchmark results

English | [简体中文](plotting_zh.md) · [Evaluation and profiling](../README.md)

Use `foretoken plot` to turn saved measurements into performance curves, comparisons, and revenue estimates. Run it on the machine holding the results. Replace the example paths below with your result or experiment directories.

```bash
foretoken plot results/baseline
```

The command exports PDF, SVG, PNG, and CSV files to `results/baseline/plots/`. It also accepts a folder containing several runs. Multiple input directories and `--base` use `./plots`; set `--output-dir` to choose another destination.

## Compare performance

Compare two approaches:

```bash
foretoken plot results/baseline results/candidate
```

Performance comparisons include output throughput per user versus output throughput per GPU. Each method has its own markers; labels such as `c1` and `c16` show the configured concurrency. Dashed curves connect Pareto-optimal points, where neither coordinate can improve without worsening the other. All measured markers remain visible.

The default curve varies `max_concurrency`. Other conditions, including input lengths, output limits, and datasets, stay in separate figures. For a request-rate scan, use `--sweep-axis request_rate --label-by request_rate`. `--label-by` accepts several saved workload fields; `--label-values` adds coordinates, and `--columns 2` produces wider figures.

### Choose axes

For P95 decode speed against total token throughput per GPU:

```bash
foretoken plot results/baseline results/candidate \
  --x-metric decode_interactivity_p95 \
  --y-metric total_tokens_per_second_per_gpu \
  --frontier max-max --label-values --columns 2
```

`--x-metric` and `--y-metric` accept saved scalar metric names. Custom axes connect points in scan order unless `--frontier` selects an optimization direction for each axis: `max-max`, `min-max`, `max-min`, or `min-min`. `--frontier none` connects every scan point.

Choose a decode-speed percentile with `decode_interactivity_p<N>`, where `N` is between 0 and 100 and may contain decimals. For example, compare several percentiles in one command:

```bash
foretoken plot results/baseline results/candidate \
  --metric decode_interactivity_p50 decode_interactivity_p95 decode_interactivity_p99.9
```

Decode speed is based on successful requests' TPOT; total token throughput includes cached input once. Definitions and required measurements are in [derived plot metrics](metrics.md#derived-plot-metrics). When the measurement's GPU count is unavailable, supply its total GPU count with `--gpu-count N`. A conflict with the recorded capacity for that measurement is an error.

### Show changes from a baseline

```bash
foretoken plot results/candidate --base results/baseline \
  --label-values --columns 2
```

`--base` loads one reference result or iteration, including its repeated measurements. Matching workloads are connected and labeled with the x-axis percentage change. Use `--compare-axis y` for the y axis. The calculation is `(candidate / baseline - 1) × 100%`; negative latency changes and positive throughput changes indicate improvement.

## Estimate revenue and profit

Copy the [pricing example](../examples/pricing.yaml) and replace its illustrative amounts with your token prices and GPU hourly costs:

```yaml
currency: USD
input_per_million: 0.15
cached_input_per_million: 0.003
output_per_million: 0.60
gpu_hour_cost: 5.0
```

`input_per_million` prices uncached input. For different method costs, replace the scalar `gpu_hour_cost` with a mapping whose keys match the method names in the comparison CSVs:

```yaml
gpu_hour_cost:
  baseline: 5.0
  candidate: 6.0
```

Save the edited file as `pricing.yaml`, then run:

```bash
foretoken plot results/baseline results/candidate \
  --pricing pricing.yaml --min-interactivity 200 \
  --interactivity-metric decode_interactivity_p95 --columns 2
```

For each method and fixed workload, this selects the measured point with the highest mean revenue whose mean P95 decode speed reaches 200 tokens/s/user. `--interactivity-metric` chooses the speed statistic for this constraint independently of the plotted axes; its default is `decode_interactivity_p90`. Omit `--min-interactivity` to consider every point. The command uses measured points without interpolation and calculates each run's revenue and profit before combining repetitions.

Financial estimates require input, output, and cached-input usage for every successful request, plus a GPU count. Missing cache usage stays unavailable. The bars show compute cost, positive profit or loss, and total revenue; the CSV records each point's eligibility and selection status.

## Read and export results

| Output | Contents |
| --- | --- |
| `tradeoff-*.csv` | Plotted coordinates, paired sample counts, standard deviations, and available baseline changes |
| `economics-*.csv` | Prices, per-GPU-hour estimates, repeat counts, selected interactivity metric, and operating-point selection |
| `comparison-summary.csv` | Scalar metric statistics for each method and workload |
| `comparison-base.csv`, with `--base` | Baseline and candidate values, relative changes, failures, condition differences, and result paths |

Error bars show sample standard deviation across repeated runs; one repetition has no error estimate. Trade-off axes and revenue selection with a speed floor use the same eligible repetitions for both measurements. Scalar tables summarize each metric's available repetitions independently. Missing values and a zero baseline leave the corresponding comparison unavailable, with a status in the CSV.

Use the names shown in the legend with `--method` to select methods; an explicit `--base` remains included. For one run, request distributions and run-level summaries can be selected together:

```bash
foretoken plot results/baseline --metric ttft generation_tokens_per_second
```

This exports the first-token latency distribution and an output-throughput summary. Cross-run performance comparisons use run-level metrics, such as `generation_tokens_per_second` and `ttft_p95_seconds`. Explicit trade-off options or `--pricing` produce the requested figures and tables; add `--metric` to include other metric figures alongside them. See [quality results](eval/README.md#read-scores) for evaluator scores.

Add `--sync_wandb` to upload saved results and single-run figures to their original W&B runs. Cross-run comparison figures remain local. See [W&B result uploads](perf/wandb.md#resume-an-upload) for recovery and publication behavior.
