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
├── workspace/                 # Agent-managed supporting files, created as needed
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

### Agent-managed supporting files

Keep research material, analysis scripts, preparation logs, review evidence and cleanup records under `<output-dir>/workspace/`, rather than adding auxiliary directories at the experiment root. Agents create this directory as needed and choose its internal layout; names such as `research/`, `preparation/`, `review/` and `cleanup/` are examples, not required subdirectories. Benchmark commands do not create `workspace/`.

Keep the report, required experiment notes and original run artifacts in their documented locations. Summarize findings in the notes and report, linking supporting files in `workspace/`. Preserve cited evidence when removing temporary files at task completion.

## Analyze results

The table below lists common experiment files. Paths start at `results/decode/parallelism/iterations/tp2/runs/perf-000001/` in the example above: `generated/` holds command and source records, while evaluation files such as `config.json` live in `artifacts/<result-directory>/`. Depending on the evaluation and output options, there may also be warmup records, GPU allocation data, Prometheus observations, and evaluator reports. Consult them as needed for your analysis.

| Order | Read | What to examine |
| --- | --- | --- |
| 1 | `generated/context.json` | Whether the command completed, its exit status, and the source record for this run |
| 2 | `config.json`, `environment.json` | Compare workloads, generation settings, client versions and service configurations. For Foretoken deployments, also check recorded service changes and code versions |
| 3 | `metrics.json`, result figures | Changes in success counts, actual output lengths, and target metrics, and whether they support the hypothesis |
| 4 | `raw_output.json`; `responses.jsonl` with `raw` enabled | Find anomalous requests and use `request_id` to match measurements, stop reasons, and actual answers; for quality evaluations, inspect native reports and saved per-sample records under `native/` |
| 5 | `generated/run.log` and the result directory's `run.log` with `quiet` enabled; `evaluator.log` for quality evaluations | Locate errors in client preparation and execution. Use existing resource observations and profiles to explain performance differences and decide what to measure next. Check the corresponding server logs for model-loading or engine errors. |

Compare saved performance runs with an explicit base using the [plotting examples](../benchmarks/docs/recipes.md#plot-saved-results). Use the generated values, repeat statistics, and condition differences alongside the records above to assess the hypothesis and identify any needed follow-up measurements.

## Complete the experiment notes

Keep execution evidence, the history of judgments, and the current summary distinct:

| Record | How it changes |
| --- | --- |
| Completed run artifacts | Preserve the original commands, metrics, responses, and logs. Later analysis links to them. In-progress files remain owned by their runner. |
| `iterations/<name>/notes/iteration.md` | The assigned Experimenter appends run interpretations, independent reviews, decisions, and corrections, with timestamps, sources, and evidence links. |
| `notes/experiment.md` | The designated writer updates the current approach, open questions and disagreements, and resource arrangements, linking the relevant iteration entries and decisions. |

Attribute analysis, reviews, and decisions to their authors. Each decision states the action and its scope. When correcting a conclusion or changing a decision, link the earlier entry and new evidence so readers can follow how the understanding changed.

After each run, append its directory or W&B link, the hypothesis tested, the comparison artifact when available, and the supported conclusion. Record failures and interruptions as well. Use the [iteration template](templates/iteration-template.md) for the history. Send contributions to the assigned writer so parallel agents do not overwrite one another's entries.

The overview is a current reading aid, not a second decision history. Refresh it as decisions change and link to the supporting entries. Reusable knowledge can also be revised as understanding improves, with links back to the experiment history.

## Maintain the user-facing report

At experiment start, the lead or a designated writer creates `report.md` using the [report template](templates/report-template.md) and shares its path with the user. Use the task's delivery location, or the experiment root when none is specified. Link the report at the start of `notes/experiment.md` and keep the same report when resuming. The writer maintains the report; benchmark commands save the underlying evidence.

Update the report after each measured iteration and whenever analysis changes a conclusion or the retained approach. Start with measured task metrics and changes from base, briefly explain the iterations, and describe the currently retained approach with result links. Write for readers who did not participate in the experiment, separating supported conclusions from hypotheses and pending validation. Before measurements are available, state the objective, starting conditions and what remains unmeasured. Keep execution details and decision history in the linked iteration notes.

On pause, stop or completion, update the same report with the reason, results obtained, retained changes and unfinished validation. Replace obsolete plans with the actual state and share the report path again.
