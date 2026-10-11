<!--
SPDX-License-Identifier: Apache-2.0
SPDX-FileCopyrightText: Copyright contributors to the Foretoken project
-->

# Experiment Records Guide

English | [简体中文](experiment-records_zh.md)

## Choose an experiment command

Choose a command from the [experiment command reference (recipes)](../benchmarks/docs/recipes.md) for your optimization goal, then adjust the workload and parameters for the model and hypothesis you want to test.

For example:

| What to measure | Recipe |
| --- | --- |
| Performance across input/output lengths and concurrency levels | [Input length, output length, and concurrency](../benchmarks/docs/recipes.md#input-length-output-length-and-concurrency) |
| The effect of longer inputs | [Long-context performance](../benchmarks/docs/recipes.md#long-context-performance) |
| Performance under multi-turn conversation traffic | [Conversations started per second](../benchmarks/docs/recipes.md#conversations-started-per-second) |
| Traffic capacity while meeting latency targets | [SLO attainment and goodput](../benchmarks/docs/recipes.md#slo-attainment-and-goodput) |
| Performance with real request arrival times | [Timestamped trace replay](../benchmarks/docs/recipes.md#timestamped-trace-replay) |

## Save experiment records

Adjust these options in the recipe command:

| Option | How to use it |
| --- | --- |
| `--output` | Combine with commas: `experiment` saves experiment records, including local results; `plot` exports figures; `wandb` uploads results; `quiet` writes progress and summaries to logs while keeping errors visible; `raw` saves actual requests and responses for text HTTP performance evaluations. |
| `--output-dir` | The agent chooses an experiment root, preferably `results/<goal>/<experiment-name>`. Use the same directory for different approaches within an experiment. |
| `--iteration` | The agent names the approach. Reuse the name for additional runs of that approach and change it when trying a different approach. Each run gets its own record; omitting this option creates a numbered iteration automatically. |

When logged in to W&B, use `experiment,wandb,plot` so developers and agents can inspect and compare results. Otherwise, use `experiment,plot` to save records and figures locally. Add `raw` when you need to inspect actual inputs and answers.

For example, when logged in to W&B, replace the recipe's output options with:

```bash
--output experiment,wandb,plot \
--output-dir results/decode/parallelism \
--iteration tp2
```

## Find experiment records

`--output-dir` selects the experiment directory, and `--iteration` names the approach:

```text
results/decode/parallelism/
├── notes/experiment.md
└── iterations/tp2/
    ├── notes/iteration.md
    └── runs/perf-000001/
        ├── generated/
        │   └── context.json
        └── artifacts/
            └── <result-directory>/
                ├── config.json
                ├── environment.json
                ├── metrics.json
                └── …
```

Records are saved on the machine running the evaluation. Relative paths start from the command's working directory. For remote runs, inspect the printed paths on the remote machine. A parameter sweep can produce several result directories within one run. With W&B enabled, the printed links also let you inspect curves and compare results.

## Analyze results

The table below lists common experiment files. Paths start at `results/decode/parallelism/iterations/tp2/runs/perf-000001/` in the example above: `generated/` holds command and source records, while evaluation files such as `config.json` live in `artifacts/<result-directory>/`. Depending on the evaluation and output options, there may also be warmup records, GPU allocation data, Prometheus observations, and evaluator reports. Consult them as needed for your analysis.

| Order | Read | What to examine |
| --- | --- | --- |
| 1 | `generated/context.json` | Whether the command completed, its exit status, and the source record for this run |
| 2 | `config.json`, `environment.json` | Whether workloads, generation settings, client versions, and service configurations are comparable; Foretoken configuration mode also records service images, Pods, resource declarations, runtime identity changes and available build-source provenance |
| 3 | `metrics.json`, result figures | Changes in success counts, actual output lengths, and target metrics, and whether they support the hypothesis |
| 4 | `raw_output.json`; `responses.jsonl` with `raw` enabled | Find anomalous requests and use `request_id` to match measurements, stop reasons, and actual answers; for quality evaluations, inspect native reports and saved per-sample records under `native/` |
| 5 | `generated/run.log` and the result directory's `run.log` with `quiet` enabled; `evaluator.log` for quality evaluations | Locate errors in client preparation and execution. Use existing resource observations and profiles to explain performance differences and decide what to measure next. Check the corresponding server logs for model-loading or engine errors. |

See [plotting examples](../benchmarks/docs/recipes.md#plot-saved-results) to redraw or compare saved results.

## Complete the experiment notes

After each run, append to the iteration's `notes/iteration.md`:

- The run directory or W&B link, and the change or hypothesis tested.
- Differences from the reference results, an explanation, and conclusions supported by the evidence.
- The decision to keep, refine, or revert the change, and the next experiment.

At the end of an iteration, summarize the main findings and link the iteration in `notes/experiment.md`. Keep metrics and logs in their original run directories and link to them from the notes. Record the reasons for failed or interrupted runs as well. Use the [experiment and iteration templates](templates/README.md) as a writing reference.
