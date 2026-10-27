# Performance metrics

English | [简体中文](metrics_zh.md) · [Performance examples](perf/README.md)

Results include aggregate metrics and per-request records.

## Request metrics

| Metric | Meaning |
| --- | --- |
| Recorded requests (`request_num`) | Requests with a recorded success or failure; requests still in flight when a run is interrupted may have no record |
| Prepared requests (`planned_num`) | HTTP requests prepared before measurement, counting each conversation turn separately; this may differ from `num_prompts`. Unavailable when the run is limited only by duration |
| Admitted requests (`admitted_num`) | Independent requests admitted by the scheduler, or the total turns in admitted conversations |
| Not executed (`not_executed_num`) | Prepared requests without a result record; when the total request count is unbounded, counts only remaining turns in admitted conversations |
| Success rate | `success_num / request_num`; skipped turns are not failed HTTP requests |
| Concurrency limit (`max_concurrency`) | Configured limit: requests for single-turn and trace workloads, conversations for multi-turn workloads |
| Observed request concurrency (`request_concurrency`) | `peak`: highest simultaneous active request count; `mean`: total request duration divided by measured run duration, including failed requests |
| End-to-end latency (E2EL) | Request duration; successful streamed requests end at the last chunk with non-empty `choices` |
| TTFT | Request start to the first chunk with non-empty `choices` |
| TPOT | `(E2EL − TTFT) / (output tokens − 1)`; zero for one output token; unavailable without output usage or streamed timing |
| ITL | Intervals between chunks with non-empty `choices`; a chunk may contain multiple tokens |
| Request throughput (req/s) | Successful requests divided by run duration |
| Input token throughput (tokens/s) | Successful requests' input tokens divided by run duration |
| Output token throughput (tokens/s) | Successful requests' output tokens divided by run duration |
| Total output tokens (`total_output_tokens`) | Actual output tokens from successful measured requests, excluding warmup and failures; unavailable if any successful request lacks usage, and 0 when none succeeded |
| Output tok/s / user | Output throughput divided by `--max-concurrency`; with `--max-concurrency -1`, uses measured average active requests |
| Output token throughput per GPU (tokens/s) | Output throughput divided by the model's declared GPU capacity |
| Mean reported cached input tokens | Mean `usage.prompt_tokens_details.cached_tokens` among successful requests that report it |
| Benchmark duration (s) | Elapsed time from starting the workload until all sent requests complete |

A failed conversation turn skips the remaining turns without replacement. `not_executed_reasons` distinguishes these skips (`failed_predecessor`) from requests excluded by the time limit (`duration`). `failure_reasons` groups recorded failures by HTTP status and error message.

Conversation workloads also report attempted and completed conversations, HTTP turns, average turns per conversation, and conversation starts per second. Successful requests and successfully completed conversations are counted separately.

Request latency distributions use successful requests. `--no-stream` retains latency and throughput but omits TTFT, TPOT, and ITL. Usage-only chunks do not advance streaming timing.

ITL percentiles have 1% relative value error. Request-level ITL SLOs compare the maximum interval.

## Curves

W&B plots input and output throughput separately for the model measured by each run. `Time` curves use one-second completion windows: a successful request contributes all its tokens when it finishes, divided by the window duration. `Cumulative` curves divide completed successful requests' token totals by elapsed time. Both measure this benchmark's traffic; Grafana model totals include all traffic reaching the model.

Time curves also show request throughput, in-flight requests, failure rate and latency percentiles. Per-request curves use send order. Compare runs in the same group, and use sweep trade-off charts to compare throughput and latency.

## Derived plot metrics

`foretoken plot` derives these metrics from saved results without changing the original measurements:

| Metric | Meaning |
| --- | --- |
| `decode_interactivity` with statistic `p<N>` | TPOT-based decode speed in tokens/s/user: `1 / P<N>(TPOT)`, using the nearest-rank percentile of successful requests' TPOT values. `N` may be an integer or decimal from 0 to 100, such as `50`, `95`, or `99.9`. One-token responses have no decode interval and are excluded. Complete successful-request output counts and the eligible requests' TPOT values are required |
| `total_tokens_per_second_per_gpu` | Input plus output tokens per second, divided by the recorded GPU count. Input usage already includes cached tokens |
| `cached_prompt_tokens_per_second_per_gpu` | Cached input tokens per second per GPU, requiring cached-input usage for every successful request |
| `uncached_prompt_tokens_per_second_per_gpu` | Input tokens minus cached input tokens per second per GPU, requiring complete input and cached-input usage |

The decode-speed calculation takes the reciprocal of the selected TPOT percentile, rather than a percentile of individual request speeds. It uses a distribution of requests, separate from the pooled chunk-interval distribution in `itl`, and excludes TTFT. It also differs from output throughput divided by configured concurrency. Every run's derived values are calculated before means and standard deviations across repeated runs.

CLI selections keep the metric and statistic separate. Exported records identify the resolved value with a key such as `decode_interactivity_p95`; trade-off and revenue CSVs also retain the selected metric and statistic. Timing summaries for latency, TTFT, TPOT, and ITL use the statistics available in the saved results. Per-request ITL summaries cannot reconstruct pooled ITL percentiles.

Trade-off coordinates use repetitions where both selected metrics are available. Revenue selection with a speed floor likewise uses repetitions with both revenue and speed. Their CSVs retain `paired_samples` alongside the full attempt and failure counts; scalar comparison tables report each metric's available repetitions independently.

GPU normalization uses the measurement's `gpu_count` in `metrics.json`. Older results without that field can use `environment.json`'s `before.declared_gpu_count` when its model matches the measurement. `--gpu-count` supplies a missing total capacity for the measurement; a conflict with its recorded capacity is an error. Financial plots apply explicit uncached-input, cached-input, and output prices to the corresponding token rates. Revenue per GPU-hour is `3600 × sum(tokens/s/GPU × price per million) / 1,000,000`; profit subtracts the configured GPU hourly cost. Missing token usage remains unavailable, including missing cache usage.

## AgentX metrics

`foretoken perf --agentx` retains the official profiling rates and request counts. Warmup records stay outside these aggregates. Timing fields use the `agentx_` prefix to retain the native metric definitions. Their distributions use successful profiling requests; errors remain in the request and failure counts:

| Metric | Meaning |
| --- | --- |
| `agentx_interactivity` with statistic `p<N>` | Reciprocal of the selected native token-interval percentile in seconds. Full-response ITL is preferred; the actual source is retained in `metrics.json` under `agentx.interactivity_source` |
| `agentx_latency` with `mean` or an available `p<N>` | Native request latency in seconds |
| `agentx_ttft` with `mean` or an available `p<N>` | Native time to first token in seconds |
| `agentx_itl` with `mean` or an available `p<N>` | The native token interval used for AgentX interactivity, in seconds |

Official summary percentiles are preserved. Additional interactivity percentiles use linear interpolation over successful native profiling observations, matching the official harness. Warmup and failed requests are excluded. AgentX uses native ITL; `decode_interactivity` uses Foretoken's request-level TPOT.

The shared throughput fields retain the official input and output rates, per-user output rate when session concurrency is known, and per-GPU rates when capacity is known. `total_output_tokens` sums complete completion usage from successful profiling requests. Cache and uncached-input rates split the official input rate using complete usage from all reported successful profiling requests.

Native throughput uses its profiling observation window; `benchmark_time` retains the native first-request-to-last-response duration, which can differ. AgentX GPU normalization uses declared capacity; GPU allocation timelines and Prometheus window aggregates are not exported.

Check `agentx.submission_valid` and its reasons separately from execution completion. See [AgentX replay](perf/agentx.md) for datasets, controls, and native reports.

## GPU allocation

Kustomize performance runs sample allocated GPU counts on the same elapsed-time axis as load, Ready replicas, and SLO windows. `gpu_allocation.json` preserves boundary samples, unknown intervals, and failed-read times. `metrics.json` reports `gpu_seconds` and `gpu_hours` separately for each device resource name, plus observation coverage. Full-window totals remain unavailable when coverage is incomplete; `observed_gpu_seconds` is a partial diagnostic area and is not a total. GPU resource names are never averaged or combined across NVIDIA, MetaX, or other device types. An explicitly observed zero allocation is a valid measurement.

With accelerator metrics available, `prometheus_observations.json` also records sampled physical device identities, including vendor, device ID, node and Pod. `environment.json` retains changes to Pod/container identities, images and runtime selections during the run.

## CPU and memory observations

For Kubernetes services with Prometheus, `metrics.json.resources` and W&B report mean CPU usage in cores, the fraction of CPU scheduling periods that were throttled, and cumulative throttled seconds over the measured window. These are estimates from container counter increases, including Pods replaced during the run; missing or insufficient scrape data remains unavailable.

Resource curves show CPU usage and throttled-period ratios as trailing one-minute rates, plus kernel CPU quota and container memory working set. CPU quota is shown in cores; the raw quota per scheduling period is in microseconds. Memory is in bytes. The saved queries and samples are in `prometheus_observations.json`.

## Speculative decoding observations

For a Kustomize model service with Prometheus, performance runs report accepted draft tokens divided by proposed draft tokens, accepted tokens per draft iteration, mean draft and target-forward GPU time per timed speculative step, and each stage's share of their combined measured GPU time. The target-forward stage includes batch verification forward but excludes sampling and rejection; neither stage time is request latency or a speedup estimate. The model-server counters cover all traffic to the selected service, including requests outside this benchmark.

`metrics.json` and W&B Summary use Prometheus counter increases over the measured run window to estimate these values, accounting for counter resets. Short runs without enough scrapes or services without speculative metrics leave values unavailable, not zero. The Prometheus time curves instead use trailing five-minute rates at each observation; they must not be read as the run-window summary. Repeated sweeps retain these per-run estimates and show mean and standard deviation across runs when samples are available.

## SLO results

When `--slo-params` is enabled, each request with latency-based criteria receives `slo_met` in `raw_output.json` and the W&B request-index history. The CLI, `metrics.json`, and W&B Summary record SLO attainment, request goodput, and token goodput for the same criteria. Failed requests and requests missing required timing metrics count as not meeting the SLO. Attainment is the fraction of measured requests meeting all conditions; request goodput divides their count by run duration, and token goodput divides their output token total by the same duration. When request-level criteria are set, one-second completion-window curves also show attainment and request/token goodput on the elapsed-time axis. Each window counts failed requests in its attainment denominator; windows without completions have no attainment value. The run summary still scores all measured requests together, not an average of window fractions. Adding `--slo-search` enables [SLO concurrency search](perf/slo.md), which evaluates aggregate criteria and reports the highest observed passing request peak with its configured limit and stopping reason.

Token counts remain unavailable when the service does not report them. If any successful request lacks input or output usage, aggregates that require the complete corresponding token total are unavailable rather than treating the missing value as zero. Cached input tokens preserve the service-reported value, including an explicit zero; they do not represent a storage-tier or KV-store hit rate.

Charts use seconds for TTFT and E2EL, and milliseconds for TPOT and ITL. Raw JSON timings remain in seconds. Trace results also include replay delay when a request is sent after its scheduled arrival.

Retries are disabled by default. `--max-retries N` allows up to `N` additional attempts for transient failures; retry time is included in logical request latency.
