# SLO auto-tune

English | [简体中文](slo_zh.md) · [Common commands](../examples.md)

After the [setup steps](../examples.md#setup), find the largest workload concurrency that still meets latency or throughput constraints. Foretoken owns the request budget, search, and result publication; generated single-turn execution reuses EvalScope's HTTP engine. The search preserves the selected workload schedule.

```bash
foretoken bench examples/quickstart \
  --dataset random --tokenizer-path Qwen/Qwen3-0.6B \
  --min-prompt-length 128 --max-prompt-length 512 \
  --number 100 --parallel 2 \
  --slo-params '[{"p99_latency":"<=2"}]' \
  --slo-upper-bound 32 \
  --num-runs 1 \
  --output local,wandb
```

`--parallel` is the starting concurrency. `--number` is the fixed HTTP request budget for every probe; it does not grow with concurrency. `--num-runs` repeats each probe with the same request budget and averages its metrics. Conversation workloads count every turn request, while trace replay keeps the selected trace events and timestamps fixed.

## Constraints

`--slo-params` takes a JSON array; each element is one criterion group:

- Multiple metrics in the same object: AND (all must hold)
- Different objects: independent binary searches, each reporting its own max concurrency

Overall: independent searches for `(group1 A AND group1 B)`, `(group2 C AND group2 D)`, … — groups do not merge into one pass/fail.

### AND: all metrics in one object

```bash
--slo-params '[{"avg_ttft":"<=0.05", "avg_tpot":"<=0.02"}]'
```

Find the largest concurrency where `avg_ttft <= 0.05s` **and** `avg_tpot <= 0.02s`. Both must pass for that concurrency level.

### Multiple groups: independent searches

```bash
--slo-params '[{"p99_ttft":"<0.05"}, {"p99_tpot":"<0.01"}]'
```

Separately find max concurrency for `p99_ttft < 0.05s` and for `p99_tpot < 0.01s`; each group gets its own result.

### AND + multiple groups

```bash
--slo-params '[{"avg_ttft":"<=0.05", "avg_tpot":"<=0.02"}, {"p99_latency":"<=5"}]'
```

- Group 1: `avg_ttft <= 0.05s` AND `avg_tpot <= 0.02s`
- Group 2: `p99_latency <= 5s`

Each group runs its own binary search and reports max concurrency.

### Metric names

Available metrics:

- Latency: `avg_latency`, `p50_latency`, `p95_latency`, `p99_latency`
- TTFT: `avg_ttft`, `p50_ttft`, `p95_ttft`, `p99_ttft`
- TPOT: `avg_tpot`, `p50_tpot`, `p95_tpot`, `p99_tpot`
- Throughput: `rps`, `tps`

SLO auto-tune supports generated workloads, conversation datasets, multiple datasets, and timestamp trace replay. Generated workloads search closed-loop `--parallel`; trace replay searches its in-flight concurrency cap while preserving arrival timestamps. It cannot be combined with `--sweep` or a positive `--rate`.

Results include `slo_results.json`. Each probe is stored below the SLO result directory, and W&B runs share one group with names that identify the criterion group, concurrency, and repeat.

When finished, run `foretoken delete examples/quickstart` if you deployed the Quick Start service.
