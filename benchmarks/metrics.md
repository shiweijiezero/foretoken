# Result metrics

English | [简体中文](metrics_zh.md) · [Common commands](docs/examples.md)

`metrics.json` contains aggregate results; `raw_output.json` contains per-request records. Standard workloads also retain `benchmark_data.db` and `benchmark.log`.

## Experiment records

With local output, `environment.json` records the client Python/package versions and, for a source checkout, its commit and dirty state. Kustomize runs additionally record ModelService intent, owned ModelGroup model/tokenizer revisions and runtime settings, pod image IDs, placement and node software before and after execution. These are observations of deployment state, not per-request routing attribution. Read errors are recorded as incomplete snapshots.

A URL does not expose authoritative hardware or deployment information. URL runs therefore record client information without a server environment snapshot. Save the server's actual GPU model/count, driver and inference-engine versions, weight/tokenizer revisions and engine settings beside the results. Even for Kustomize, a revision such as `main` is mutable; preserve resolved model/tokenizer commits or local artifact versions separately. Preserve local code changes when the client source is dirty. Snapshots do not reset caches, prevent rollouts or identify every transient change during a run.

`--warmup-requests N` completes N conversations before each generated run, including each sweep repetition and each dataset child. All warmup requests must succeed; local warmup results are saved under `warmup/` and are excluded from measured metrics and profiling. The default is zero. Warmup reuses the workload's starting rows/seed and can populate prefix caches; it does not reserve disjoint data or guarantee steady state. Use separate warmup commands with different rows if the experiment requires disjoint data. Trace replay requires separate warmup.

The model service keeps running between warmup and measurement. Warmup drains all outstanding requests, and measurement uses a new HTTP client, so it starts without in-flight warmup traffic and may include connection setup. This warms the server but does not preserve continuous load across the two phases.

For sweeps, `sweep_points.json` retains every repetition. `sweep_summary.json` and `sweep_summary.csv` group by parameter point and report mean, median, sample standard deviation, minimum and maximum across runs. `runs` is the number of repetitions; `samples` counts available values for that metric. Missing timings are omitted from that metric's samples, while failure counts and zero throughput remain in the summaries. Standard deviation is unavailable for fewer than two samples. A mean or median of run p95 values is **not** a pooled p95. Timing summary columns ending in `_seconds` use seconds.

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
