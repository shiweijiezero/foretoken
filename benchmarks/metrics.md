# Performance metrics

English | [简体中文](metrics_zh.md) · [Performance examples](docs/perf/README.md)

Results include aggregate metrics and per-request records.

## Request metrics

| Metric | Meaning |
| --- | --- |
| Success rate | Successful requests divided by attempted requests |
| Concurrency limit (`max_concurrency`) | Configured limit: requests for single-turn and trace workloads, conversations for multi-turn workloads |
| Observed request concurrency (`request_concurrency`) | `peak`: highest simultaneous active request count; `mean`: total request duration divided by measured run duration, including failed requests |
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

When `--slo-params` is enabled, each request with latency-based criteria receives `slo_met` in `raw_output.json` and the W&B request-index history. The CLI, `metrics.json`, and W&B Summary record SLO attainment, request goodput, and token goodput for the same criteria. [SLO concurrency search](docs/perf/slo.md) evaluates aggregate criteria and reports the highest observed passing request peak with its configured limit and stopping reason.

Token counts remain unavailable when the service does not report them. If any successful request lacks input or output usage, aggregates that require the complete corresponding token total are unavailable rather than treating the missing value as zero. Cached input tokens preserve the service-reported value, including an explicit zero; they do not represent a storage-tier or KV-store hit rate.

Conversation metrics are published only when the workload actually executes multiple turns. Each HTTP turn is a request. A failed turn stops that conversation; successful turns are not successful conversations. Multi-dataset runs keep conversation percentiles per dataset instead of averaging them.

Charts use seconds for TTFT, E2EL, and conversation timings, and milliseconds for TPOT and ITL. Raw JSON timings remain in seconds. Trace results also include replay delay when a request is sent after its scheduled arrival.

Retries are disabled by default. `--max-retries N` allows up to `N` additional attempts for transient failures; retry time is included in logical request latency.
