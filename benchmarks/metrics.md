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
| TPOT | `(E2EL − TTFT) / (output tokens − 1)`; zero for one output token; unavailable without output usage or streamed timing |
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

## Curves

W&B plots input and output throughput separately for the model measured by each run. `Time` curves use one-second completion windows: a successful request contributes all its tokens when it finishes, divided by the window duration. `Cumulative` curves divide completed successful requests' token totals by elapsed time. Both measure this benchmark's traffic; Grafana model totals include all traffic reaching the model.

Time curves also show request throughput, in-flight requests, failure rate and latency percentiles. Per-request curves use send order. Compare runs in the same group, and use sweep Pareto charts to compare throughput and latency trade-offs.

## GPU allocation

Kustomize performance runs sample allocated GPU counts on the same elapsed-time axis as load, Ready replicas, and SLO windows. `gpu_allocation.json` preserves boundary samples, unknown intervals, and failed-read times. `metrics.json` reports `gpu_seconds` and `gpu_hours` separately for each device resource name, plus observation coverage. Full-window totals remain unavailable when coverage is incomplete; `observed_gpu_seconds` is a partial diagnostic area and is not a total. GPU resource names are never averaged or combined across NVIDIA, MetaX, or other device types. An explicitly observed zero allocation is a valid measurement.

## Speculative decoding observations

For a Kustomize model service with Prometheus, performance runs report accepted draft tokens divided by proposed draft tokens, accepted tokens per draft iteration, mean draft and target-forward GPU time per timed speculative step, and each stage's share of their combined measured GPU time. The target-forward stage includes batch verification forward but excludes sampling and rejection; neither stage time is request latency or a speedup estimate. The model-server counters cover all traffic to the selected service, including requests outside this benchmark.

`metrics.json` and W&B Summary use Prometheus counter increases over the measured run window to estimate these values, accounting for counter resets. Short runs without enough scrapes or services without speculative metrics leave values unavailable, not zero. The Prometheus time curves instead use trailing five-minute rates at each observation; they must not be read as the run-window summary. Repeated sweeps retain these per-run estimates and show mean and standard deviation across runs when samples are available.

## SLO results

When `--slo-params` is enabled, each request with latency-based criteria receives `slo_met` in `raw_output.json` and the W&B request-index history. The CLI, `metrics.json`, and W&B Summary record SLO attainment, request goodput, and token goodput for the same criteria. Failed requests and requests missing required timing metrics count as not meeting the SLO. Attainment is the fraction of measured requests meeting all conditions; request goodput divides their count by run duration, and token goodput divides their output token total by the same duration. When request-level criteria are set, one-second completion-window curves also show attainment and request/token goodput on the elapsed-time axis. Each window counts failed requests in its attainment denominator; windows without completions have no attainment value. The run summary still scores all measured requests together, not an average of window fractions. Adding `--slo-search` enables [SLO concurrency search](docs/perf/slo.md), which evaluates aggregate criteria and reports the highest observed passing request peak with its configured limit and stopping reason.

Token counts remain unavailable when the service does not report them. If any successful request lacks input or output usage, aggregates that require the complete corresponding token total are unavailable rather than treating the missing value as zero. Cached input tokens preserve the service-reported value, including an explicit zero; they do not represent a storage-tier or KV-store hit rate.

Conversation metrics are published only when the workload actually executes multiple turns. Each HTTP turn is a request. A failed turn stops that conversation; successful turns are not successful conversations. Multi-dataset runs keep conversation percentiles per dataset instead of averaging them.

Charts use seconds for TTFT, E2EL, and conversation timings, and milliseconds for TPOT and ITL. Raw JSON timings remain in seconds. Trace results also include replay delay when a request is sent after its scheduled arrival.

Retries are disabled by default. `--max-retries N` allows up to `N` additional attempts for transient failures; retry time is included in logical request latency.
