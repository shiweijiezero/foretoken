# Result metrics

English | [简体中文](metrics_zh.md) · [Common commands](docs/examples.md)

`metrics.json` contains aggregate results; `raw_output.json` contains per-request records. Standard workloads also retain `benchmark_data.db` and `benchmark.log`.

## Experiment records

With local output, `environment.json` records the client Python/package versions and the source checkout's commit and dirty state when available. For Kustomize deployments it also records model-service settings, model/tokenizer revisions, runtime configuration, pod image IDs and node information before and after execution. A snapshot's `error` field identifies a failed read. URL runs contain client information only. For reproducible configuration comparisons, see [Parameter sweeps](docs/coomon_commands/sweep.md#compare-inference-configurations).

`--warmup-requests N` completes N conversations before each generated run, including each sweep repetition and dataset child. Its results are saved under `warmup/`, separate from measured metrics and profiling. Warmup reuses the starting rows and seed; measurement begins after all warmup requests succeed and opens a new HTTP client. The default is zero. Trace replay requires separate warmup.

For sweeps, `sweep_points.json` retains every repetition. `sweep_summary.json` and `sweep_summary.csv` group results by parameter point:

| Summary field | Meaning |
| --- | --- |
| `runs` | Total repetitions, including failed runs |
| `samples` | Available values for this metric; missing timings are omitted, while zero throughput and failure counts are retained |
| `mean`, `median`, `min`, `max` | Statistics across the available run-level values |
| `stddev` | Sample standard deviation; unavailable for fewer than two samples |

Timing metric names ending in `_seconds` use seconds. Statistics of per-run p95 values describe variation between runs, not a percentile calculated from pooled requests.

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
| Output token throughput per user (tokens/s) | Output throughput divided by configured concurrency; `--parallel -1` uses total output throughput |
| Output token throughput per GPU (tokens/s) | Output throughput divided by the model's declared GPU capacity, used in sweeps |
| Benchmark duration (s) | Duration of the whole benchmark run |

Request latency distributions use successful requests. `--no-stream` retains latency and throughput but omits TTFT, TPOT, and ITL. Usage-only chunks do not advance streaming timing.

For multi-turn data, each HTTP turn is a request. A failed turn stops that conversation; successful turns are not successful conversations. Multi-dataset runs keep conversation percentiles per dataset instead of averaging them.

## Curves

W&B records these views after each run:

- Time series use elapsed seconds for one-second completion-window counts, throughput, failure rate, p95 timings, and mean in-flight requests. The last window uses its actual duration.
- Cumulative series show completed-request totals, success rate, mean timings, and throughput since the run began.
- Request series use request index in send order, starting at one, for individual timings, token counts, and success.
- Kustomize runs also record controller-applied desired and Ready replicas for each model service and scaling target.

Charts and console output use seconds for TTFT, E2EL, and conversation timings, and milliseconds for TPOT and ITL. Raw JSON timings remain in seconds.

Time-window token throughput attributes a successful request's tokens to the window in which it finishes; it is not a measurement of individual token emission times. Windows without completions have zero throughput but no latency or failure-rate sample. These histories are uploaded after completion, not streamed live.

Runs in the same W&B group share these axes. Final aggregates and available p50/p95/p99 values are also recorded in Charts for run comparisons, with a copy in Summary. Older runs retain their original metric names and units.

Trace results also report replay delay from scheduled arrival to actual send. E2EL and TTFT labeled `including replay delay` include that wait. Trace series use scheduled arrival time; time series use actual request completion windows.

Retries are disabled by default. `--max-retries N` allows up to `N` additional attempts for transient failures; retry time is included in logical request latency.
