# Performance metrics

English | [简体中文](metrics_zh.md) · [Common commands](docs/examples.md)

`metrics.json` contains aggregate results; `raw_output.json` contains per-request records. Standard workloads also retain `benchmark_data.db` and `benchmark.log`.

## Experiment records

Local output includes:

| File | Content |
| --- | --- |
| `environment.json` | Client versions and source state; Kustomize runs also include serving settings, image IDs and nodes before/after execution. Failed reads have an `error` field. |
| `prometheus_observations.json` | Kustomize-run Prometheus samples for model-server, GPU, KV, routing, and queue metrics when a compatible Prometheus is available. |
| `warmup/` | Warmup results, excluded from measured metrics and profiling |
| `sweep_points.json` | Every sweep repetition |
| `sweep_summary.json`, `sweep_summary.csv` | Per-point mean, median, sample standard deviation and range across repetitions |

In sweep summaries, `runs` counts repetitions and `samples` counts available values. Missing timings are omitted; zero throughput and failure counts remain. `stddev` is unavailable for fewer than two samples. Timing metrics ending in `_seconds` use seconds. Summaries of run p95 values are not pooled request percentiles.

Warmup reuses the workload's starting rows and seed and must succeed before measurement begins. Trace replay warms selected leading events, then replays the measured trace from its original clock.

## Request metrics

| Metric | Meaning |
| --- | --- |
| Success rate | Successful requests divided by attempted requests |
| End-to-end latency (E2EL) | Request duration; successful streamed requests end at the last chunk with non-empty `choices` |
| TTFT | Request start to the first chunk with non-empty `choices` |
| TPOT | `(E2EL − TTFT) / (output tokens − 1)`; unavailable for fewer than two output tokens |
| ITL | Intervals between chunks with non-empty `choices`; a chunk may contain multiple tokens |
| Time to final-answer token (TTFAT) | Conversation start to the first chunk of its final answer |
| Request throughput (req/s) | Successful requests divided by run duration |
| Input token throughput (tokens/s) | Successful requests' input tokens divided by run duration |
| Output token throughput (tokens/s) | Successful requests' output tokens divided by run duration |
| Output tok/s / user | Output throughput divided by `--max-concurrency`; with `--max-concurrency -1`, uses measured average active requests |
| Output token throughput per GPU (tokens/s) | Output throughput divided by the model's declared GPU capacity |
| Mean reported cached input tokens | Mean `usage.prompt_tokens_details.cached_tokens` among successful requests that report it |
| Benchmark duration (s) | Duration of the whole benchmark run |

Request latency distributions use successful requests. `--no-stream` retains latency and throughput but omits TTFT, TPOT, and ITL. Usage-only chunks do not advance streaming timing.

## SLO results

When `--slo-params` is enabled, each request with latency-based criteria receives `slo_met` in `raw_output.json` and the W&B request-index history. The CLI, `metrics.json`, and W&B Summary record SLO attainment, request goodput, and token goodput for the same criteria. Probe-level SLO capacity search still evaluates the configured aggregate criteria and searches the largest satisfying concurrency.

Token counts remain unavailable when the service does not report them. If any successful request lacks input or output usage, aggregates that require the complete corresponding token total are unavailable rather than treating the missing value as zero. Cached input tokens preserve the service-reported value, including an explicit zero; they do not represent a storage-tier or KV-store hit rate.

Conversation metrics are published only when the workload actually executes multiple turns. Each HTTP turn is a request. A failed turn stops that conversation; successful turns are not successful conversations. Multi-dataset runs keep conversation percentiles per dataset instead of averaging them.

## Curves

W&B records these views after each run:

- Time series use elapsed seconds for one-second completion-window counts, throughput, failure rate, p95 timings, and mean in-flight requests. The last window uses its actual duration.
- Cumulative series show completed-request totals, success rate, mean timings, and throughput since the run began.
- Request series use request index in send order, starting at one, for individual timings, reported token counts, success, and `slo_met` when SLO criteria are enabled.
- Kustomize runs also record controller-applied desired and Ready replicas for each model service and scaling target; Prometheus observations are uploaded as a W&B benchmark artifact when available.

Charts and console output use seconds for TTFT, E2EL, and conversation timings, and milliseconds for TPOT and ITL. Raw JSON timings remain in seconds.

Time-window token throughput attributes a successful request's tokens to the window in which it finishes; it is not a measurement of individual token emission times. Windows without completions have zero throughput but no latency or failure-rate sample. These histories are uploaded after completion, not streamed live.

Runs in the same W&B group share these axes. Final aggregates and available p50/p95/p99 values are also recorded in Charts for run comparisons, with a copy in Summary. Older runs retain their original metric names and units.

Trace results also report replay delay from scheduled arrival to actual send. E2EL and TTFT labeled `including replay delay` include that wait. Trace series use scheduled arrival time; time series use actual request completion windows.

Retries are disabled by default. `--max-retries N` allows up to `N` additional attempts for transient failures; retry time is included in logical request latency.
