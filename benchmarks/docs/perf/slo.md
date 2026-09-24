# SLO concurrency search

English | [简体中文](slo_zh.md) · [Performance examples](README.md)

After [setup](README.md#setup), find the largest client concurrency that meets a service-level objective (SLO), such as a latency or throughput target:

```bash
foretoken perf examples/quickstart \
  --dataset random --tokenizer-path Qwen/Qwen3-0.6B \
  --min-prompt-length 128 --max-prompt-length 512 \
  --num-prompts 100 --max-concurrency 2 \
  --slo-params '[{"p99_latency":"<=2"}]' \
  --slo-upper-bound 32 \
  --num-runs 1 \
  --output local,wandb
```

This starts at concurrency 2 and searches up to 32, requiring p99 request latency at or below two seconds. Each probe sends the same `--num-prompts` request budget. `--num-runs` repeats each probe and averages its metrics.

The search varies client concurrency while preserving the selected arrival process. It supports generated, multi-turn, and multi-dataset workloads. Every conversation turn counts as a request. A parameter sweep can also run a separate SLO search at each point.

## Set criteria

`--slo-params` accepts a JSON array. Conditions in one object must all hold; separate objects run independent searches.

| JSON value | Search result |
| --- | --- |
| `[{"avg_ttft":"<=0.05", "avg_tpot":"<=0.02"}]` | Largest concurrency meeting both timing targets |
| `[{"p99_ttft":"<0.05"}, {"p99_tpot":"<0.01"}]` | One result for the TTFT target and another for TPOT |
| `[{"avg_ttft":"<=0.05", "avg_tpot":"<=0.02"}, {"p99_latency":"<=5"}]` | One result meeting both mean timing targets and another for p99 latency |

Timing thresholds use seconds. Supported metrics are:

| Metric | Names |
| --- | --- |
| Request latency | `avg_latency`, `p50_latency`, `p95_latency`, `p99_latency` |
| Time to first token | `avg_ttft`, `p50_ttft`, `p95_ttft`, `p99_ttft` |
| Time per output token | `avg_tpot`, `p50_tpot`, `p95_tpot`, `p99_tpot` |
| Throughput | `rps` (requests/s), `tps` (output tokens/s) |

## Read results

`slo_results.json` records the probes and largest satisfying concurrency for each criterion group. Each probe has its own result directory. W&B runs share a group, with names identifying the criteria, concurrency, and repetition.

If you explicitly deployed the Quick Start service, remove it with `foretoken delete examples/quickstart` when it is no longer needed.
