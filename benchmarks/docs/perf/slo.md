# SLO concurrency search

English | [简体中文](slo_zh.md) · [Performance examples](README.md)

After [setup](README.md#setup), increase the client concurrency limit and measure the highest observed request peak that meets a service-level objective (SLO), such as a latency or throughput target:

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

This starts at concurrency limit 2 and searches up to 32, requiring p99 request latency at or below two seconds. Each probe keeps the same `--num-prompts` request budget and arrival process. `--num-runs` repeats each probe: SLO criteria use averaged metrics, while observed concurrency uses the highest request peak across repetitions. A passing probe requires every request to succeed and every required metric to be available.

The search stops when increasing the limit no longer increases the observed number of simultaneous requests, when it reaches the upper bound, or after refining an SLO failure boundary. For example, a four-request budget may reach a peak of four at limits 4 and 8; the result then reports peak 4 at limit 4.

Generated, multi-turn, multi-dataset, and trace workloads are supported. For traces, use `--trace-max-concurrency` to set the initial limit; trace timestamps determine arrivals. For multi-turn workloads, the configured limit counts conversations, while the measured peak counts requests. Every conversation turn counts toward the request budget. A parameter sweep can also run a separate SLO search at each point.

## Set criteria

`--slo-params` accepts a JSON array. Conditions in one object must all hold; separate objects run independent searches.

| JSON value | Search result |
| --- | --- |
| `[{"avg_ttft":"<=0.05", "avg_tpot":"<=0.02"}]` | Highest observed request peak meeting both timing targets |
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

The console and `slo_results.json` report each criterion group's highest passing request peak, its configured limit, the last measured peak and limit, and the stopping reason. These results describe the selected workload and arrival rate. Each probe has its own result directory. W&B includes a search summary and grouped probe runs identified by criteria group, concurrency limit, and repetition.

If you explicitly deployed the Quick Start service, remove it with `foretoken delete examples/quickstart` when it is no longer needed.
