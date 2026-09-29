# SLO concurrency search

English | [简体中文](slo_zh.md) · [Performance examples](README.md)

After [setup](README.md#setup), increase the client concurrency limit and measure the highest observed request peak that meets a service-level objective (SLO), such as a latency or throughput target:

```bash
foretoken perf examples/quickstart \
  --dataset random \
  --min-prompt-length 128 --max-prompt-length 512 \
  --num-prompts 100 --max-concurrency 2 \
  --slo-search --slo-params '[{"p99_latency":"<=2"}]' \
  --slo-upper-bound 32 \
  --num-runs 1 \
  --output local,wandb
```

This starts at concurrency limit 2 and searches up to 32, requiring p99 request latency at or below two seconds. Each probe keeps the same `--num-prompts` request budget and arrival process. `--num-runs` repeats each probe: SLO criteria use averaged metrics, while observed concurrency uses the highest request peak across repetitions. A passing probe requires every request to succeed and every required metric to be available.

The search stops at the SLO boundary or configured upper bound, or earlier if a higher limit produces no increase in simultaneous requests. For example, a four-request budget may reach a peak of four at limits 4 and 8; the result then reports peak 4 at limit 4.

For traces, use `--max-concurrency` for the in-flight request limit; arrivals follow trace timestamps. For multi-turn workloads, the limit counts conversations, while the measured peak and request budget count individual requests.

## Measure attainment at fixed request rates

The [DistServe ShareGPT configuration](../../scripts/common/distserve-sharegpt.jsonl) follows the official artifact's history-prefix sampling and per-request output lengths. Prepare the requests using the tokenizer of the model being served, then replace `http://host/v1/completions` below with that service's endpoint:

```bash
python benchmarks/scripts/prepare_distserve_sharegpt.py --model facebook/opt-13b

foretoken perf --url http://host/v1/completions --model facebook/opt-13b \
  --sweep benchmarks/scripts/common/distserve-sharegpt.jsonl \
  --slo-params '[{"ttft":"<=0.25","tpot":"<=0.1"}]' \
  --num-runs 3 --experiment-name distserve-sharegpt --output local,wandb,plot
```

Preparation downloads the artifact's ShareGPT source. For each conversation with at least three messages, it selects a random history prefix, joins its message values with newlines, and measures the next recorded message's token length. It retains the artifact's short-sequence filter and input-plus-output bound below 2048 tokens, then samples 300 requests with seed 0. The generated JSONL sends token IDs to Completions without a chat template and requests each sample's recorded output length exactly. Keep the served model and preparation tokenizer identical; `--tokenizer` selects a separate tokenizer repository or local directory when needed.

The sweep uses the artifact's OPT-13B DistServe rates: 0.75, 1.5, 3, 4.5, 6, 6.75, 7.5, and 9 requests/s, with Poisson arrivals, no client concurrency limit, temperature 1, and no added warmup. The command repeats each point three times and scores TTFT at or below 250 ms and TPOT at or below 100 ms. `--slo-params` scores each fixed load; only `--slo-search` enables concurrency search.

Protocol sources: [dataset preparation](https://github.com/LLMServe/DistServe/blob/main/evaluation/2-benchmark-serving/0-prepare-dataset.py), [sampling and arrivals](https://github.com/LLMServe/DistServe/blob/main/evaluation/2-benchmark-serving/2-benchmark-serving.py), and [rate settings](https://github.com/LLMServe/DistServe/blob/main/evaluation/ae-scripts/e2e/opt-13b-distllm-client.sh). Measurements use the service's OpenAI streaming endpoint rather than the artifact's custom timestamp response.

Measurement accepts one criteria object using `latency`, `ttft`, `tpot`, or `itl`, in seconds. Every condition must hold for a request to meet its SLO; failed requests and requests missing a required metric do not meet it. `itl` checks the maximum observed chunk interval of each request, not a global p99 token interval.

The sweep exports arrival-rate curves for attainment, request goodput, token goodput, and latency. Read the highest tested rate meeting the selected attainment target (for example, 90% or 99%) from the results, and extend or refine the rate list in the parameter file to locate the boundary. Each run's fraction is computed separately before averaging; this is not a pooled-request fraction or an automatic capacity search.

## Set search criteria

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

The console and `slo_results.json` report each criterion group's highest passing request peak, its configured limit, the last measured peak and limit, and the stopping reason.

If you explicitly deployed the Quick Start service, remove it with `foretoken delete examples/quickstart` when it is no longer needed.
