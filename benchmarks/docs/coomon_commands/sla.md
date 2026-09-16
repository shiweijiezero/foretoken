# SLA auto-tune

English | [简体中文](sla_zh.md) · [Common commands](../examples.md)

After the [setup steps](../examples.md#setup), find the largest concurrency that still meets latency or throughput constraints. Search and `--sla-params` parsing reuse EvalScope; Foretoken publishes results. Only closed-loop `--parallel` is tuned (not arrival rate).

```bash
foretoken bench examples/quickstart \
  --dataset random --tokenizer-path Qwen/Qwen3-0.6B \
  --min-prompt-length 128 --max-prompt-length 512 \
  --parallel 2 \
  --sla-params '[{"p99_latency":"<=2"}]' \
  --sla-upper-bound 32 \
  --num-runs 1 \
  --output local,wandb
```

`--parallel` is the search start value. Passing `--sla-params` enables the search. Each probe uses `number = round(parallel * multiplier)` (default multiplier 2), so a plain `--number` is not the per-probe budget during SLA search. `--num-runs` averages that many runs at each concurrency probe. Omit `--rate` or keep `--rate -1`.

## Constraints

`--sla-params` takes a JSON array; each element is one criterion group:

- Multiple metrics in the same object: AND (all must hold)
- Different objects: independent binary searches, each reporting its own max concurrency

Overall: independent searches for `(group1 A AND group1 B)`, `(group2 C AND group2 D)`, … — groups do not merge into one pass/fail.

### AND: all metrics in one object

```bash
--sla-params '[{"avg_ttft":"<=0.05", "avg_tpot":"<=0.02"}]'
```

Find the largest concurrency where `avg_ttft <= 0.05s` **and** `avg_tpot <= 0.02s`. Both must pass for that concurrency level.

### Multiple groups: independent searches

```bash
--sla-params '[{"p99_ttft":"<0.05"}, {"p99_tpot":"<0.01"}]'
```

Separately find max concurrency for `p99_ttft < 0.05s` and for `p99_tpot < 0.01s`; each group gets its own result.

### AND + multiple groups

```bash
--sla-params '[{"avg_ttft":"<=0.05", "avg_tpot":"<=0.02"}, {"p99_latency":"<=5"}]'
```

- Group 1: `avg_ttft <= 0.05s` AND `avg_tpot <= 0.02s`
- Group 2: `p99_latency <= 5s`

Each group runs its own binary search and reports max concurrency.

### Metric names

Available metrics:

- Latency: `avg_latency`, `p50_latency`, `p95_latency`, `p99_latency`
- TTFT: `avg_ttft`, `p50_ttft`, `p90_ttft`, `p95_ttft`, `p99_ttft`
- TPOT: `avg_tpot`, `p50_tpot`, `p90_tpot`, `p95_tpot`, `p99_tpot`
- Throughput: `rps`, `tps`

SLA auto-tune cannot be combined with `--trace`, `--sweep`, unlimited `--parallel -1`, a positive `--rate`, or multiple `--dataset` sources.

Results include `sla_results.json` and `metrics.json` with an `sla` block.

When finished, run `foretoken delete examples/quickstart` if you deployed the Quick Start service.
