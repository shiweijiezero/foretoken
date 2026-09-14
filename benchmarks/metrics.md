# Result metrics

English | [简体中文](metrics_zh.md) · [Common commands](docs/examples.md)

`metrics.json` contains aggregate results; `raw_output.json` contains per-request records. Standard workloads also retain `benchmark_data.db` and `benchmark.log`.

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
| Output token throughput per user (tokens/s) | Output throughput divided by configured concurrency; `--parallel -1` uses total output throughput |
| Output token throughput per GPU (tokens/s) | Output throughput divided by the model's declared GPU capacity, used in sweeps |
| Benchmark duration (s) | Duration of the whole benchmark run |

Request latency distributions use successful requests. `--no-stream` retains latency and throughput but omits TTFT, TPOT, and ITL. Usage-only chunks do not advance streaming timing.

For multi-turn data, each HTTP turn is a request. A failed turn stops that conversation; successful turns are not successful conversations. Multi-dataset runs keep conversation percentiles per dataset instead of averaging them.

## Curves

W&B records two views after each run:

- **Time/** uses elapsed seconds. One-second windows show successful completion throughput, failure rate, p95 request timings, and time-weighted mean in-flight requests. Completed, successful, and failed request counts are cumulative. The last window uses its actual duration.
- **Requests/** uses request index in send order, starting at one. It shows each request's E2EL, token counts, success, and available streaming timings.

Time-window token throughput attributes a successful request's tokens to the window in which it finishes; it is not a measurement of individual token emission times. Windows without completions have zero throughput but no latency or failure-rate sample. These histories are uploaded after completion, not streamed live.

Task runs in the same W&B group share these axes for comparison. Final metrics remain in Summary.

Trace results also report replay delay from scheduled arrival to actual send. E2EL and TTFT labeled `including replay delay` include that wait. **Trace/** charts use scheduled arrival time; **Time/** charts use actual request completion windows.

Retries are disabled by default. `--max-retries N` allows up to `N` additional attempts for transient failures; retry time is included in logical request latency.
