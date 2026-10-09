# SLO concurrency search

English | [简体中文](slo_zh.md) · [Performance examples](README.md)

After [setup](README.md#setup), increase the client concurrency limit and measure the highest observed request peak that meets a service-level objective (SLO), such as a latency or throughput target:

```bash
foretoken perf examples/quickstart \
  --dataset random \
  --min-prompt-length 128 --max-prompt-length 512 \
  --num-prompts 100 --max-concurrency 2 \
  --slo-search --slo-params '[{"p99_latency":"<=2s"}]' \
  --slo-upper-bound 32 \
  --num-runs 1 \
  --output local,wandb
```

This starts at concurrency limit 2 and searches up to 32, requiring p99 request latency at or below two seconds. Each probe keeps the same `--num-prompts` request budget and arrival process. `--num-runs` repeats each probe: SLO criteria use averaged metrics, while observed concurrency uses the highest request peak across repetitions. A passing probe requires every request to succeed and every required metric to be available.

The search stops at the SLO boundary or configured upper bound, or earlier if a higher limit produces no increase in simultaneous requests. For example, a four-request budget may reach a peak of four at limits 4 and 8; the result then reports peak 4 at limit 4.

For traces, use `--max-concurrency` for the in-flight request limit; arrivals follow trace timestamps. For multi-turn workloads, the limit counts conversations, while the measured peak and request budget count individual requests.

## Measure attainment at fixed conversation rates

The [conversation rate configuration](../../scripts/common/conversation-rate.jsonl) scans a starting range of 2, 4, 8, and 16 conversations/s. Replace the URL and model below with your service's Chat Completions endpoint and model:

```bash
foretoken perf --url http://host/v1/chat/completions --model Qwen/Qwen3-0.6B \
  --dataset hf://datasets/anon8231489123/ShareGPT_Vicuna_unfiltered/ShareGPT_V3_unfiltered_cleaned_split.json \
  --sweep benchmarks/scripts/common/conversation-rate.jsonl \
  --temperature 0 --random-seed 0 --max-concurrency -1 \
  --num-prompts 100 --warmup-requests 0 \
  --slo-params '[{"ttft":"<=250ms","tpot":"<=100ms"}]' \
  --num-runs 1 --experiment-name sharegpt-rate --output local,wandb,plot
```

The rate controls when conversations start, while the SLO measures each HTTP turn's TTFT and TPOT. The 100-request budget counts turns, so the last conversation may stop when the budget is reached. Turns with text reference answers generate matching token counts; see [conversation output lengths](conversations.md) for overrides. `--slo-params` scores this fixed workload; `--slo-search` enables concurrency search.

Fixed-load measurement accepts one criteria object using `latency`, `ttft`, `tpot`, or `itl`. Timing thresholds use `s` or `ms`, such as `<=2s` or `<=100ms`; unitless values use seconds. Every condition must hold for a request to meet its SLO. Failed requests and requests missing a required metric count as not meeting it. `itl` checks each request's maximum observed chunk interval.

Compare attainment, goodput (throughput of requests or tokens meeting the SLO), and latency across rates. Choose the highest tested conversation rate meeting your attainment target, such as 90% or 99%, then extend or refine the rate list to locate the boundary. With repeated runs, attainment is the mean of per-run fractions, not a fraction pooled across all requests. [One-second SLO windows](../metrics.md#slo-results) show how attainment changes during a run. To vary thresholds as well as rates, use the [threshold sweep](sweep.md#compare-slo-thresholds-and-request-rates).

## Set search criteria

`--slo-params` accepts a JSON array. Conditions in one object must all hold; separate objects run independent searches.

| JSON value | Search result |
| --- | --- |
| `[{"avg_ttft":"<=50ms", "avg_tpot":"<=20ms"}]` | Highest observed request peak meeting both timing targets |
| `[{"p99_ttft":"<50ms"}, {"p99_tpot":"<10ms"}]` | One result for the TTFT target and another for TPOT |
| `[{"avg_ttft":"<=50ms", "avg_tpot":"<=20ms"}, {"p99_latency":"<=5s"}]` | One result meeting both mean timing targets and another for p99 latency |

Supported search metrics are:

| Metric | Names |
| --- | --- |
| Request latency | `avg_latency`, `p50_latency`, `p95_latency`, `p99_latency` |
| Time to first token | `avg_ttft`, `p50_ttft`, `p95_ttft`, `p99_ttft` |
| Time per output token | `avg_tpot`, `p50_tpot`, `p95_tpot`, `p99_tpot` |
| Throughput | `rps` (requests/s), `tps` (output tokens/s) |

## Read results

The console and `slo_results.json` report each criterion group's highest passing request peak, its configured limit, the last measured peak and limit, and the stopping reason.

If you explicitly deployed the Quick Start service, remove it with `foretoken delete examples/quickstart` when it is no longer needed.
