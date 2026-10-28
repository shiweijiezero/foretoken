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
├── report.md                  # Agent-maintained overall report
├── figures/
│   ├── progress.yaml          # Chart data for one metric and comparison condition
│   ├── progress-milestones.png
│   └── progress-search.png
├── team/                      # Agent-maintained team and observation notes
│   ├── README.md              # Task, reminders and follow-up decisions
│   └── observations/          # One dated note per Observer check
└── iterations/tp2/
    ├── notes/
    │   ├── iteration.md       # Summaries, document links and decisions
    │   ├── plan.md            # Example: full proposal and execution plan
    │   └── analysis.md        # Example: detailed result analysis
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

### Share documents and code

Store each approach's complete proposals, plans and analyses in `<output-dir>/iterations/<name>/notes/`. For example, `plan.md` explains the problem and evidence, the proposed approach and alternatives, key trade-offs, and the implementation and validation plan; `analysis.md` explains the measured results. Members can begin these documents before measurement and reuse the same name with `--iteration` when running experiments.

Use a summary table in `iteration.md` to introduce each topic and link its full document. Keep dated reviews and decisions below the table. `report.md` is the overall entry point: it links the iteration notes and summarizes goals, comparisons and findings across iterations.

The lead provides all members with a readable experiment directory, its host and absolute path. Use that absolute `--output-dir` across checkouts sharing a filesystem, with `--iteration` distinguishing candidates. For records on hosts without shared storage, link their actual locations and access instructions from the report.

Code stays in its assigned checkout or worktree. The proposal names its owner, host, absolute code path, branch and revision, links relevant files or a saved diff, and notes any uncommitted changes.

Follow the [team's communication guidance](../.agents/skills/rsi/roles/common.md#share-findings-and-keep-working): brief coordination uses messages, while long explanations and material for several readers go in shared files. Send the summary and exact path, then describe later changes without repeating all the background. Discuss open choices directly with the person responsible, record decisions in the notes, and continue other work while waiting for replies. Each document has one writer; save substantial reviews or alternative proposals in the author's own files.

For example, a throughput improvement points to the reference and candidate `metrics.json` files and the compared fields. An error points to the log file and relevant lines or time range; a code finding identifies the revision, file and function. Findings about agent decisions or tool calls point to the session record and message or time range. Send the finding with these source locations so recipients can read the relevant portions first, expanding the context when needed.

Supporting research, scripts, preparation logs and cleanup records also belong under the iteration's `notes/`, organized as needed. Filenames such as `plan.md` and `analysis.md` are examples. Benchmark commands create missing iteration notes without replacing existing content; members maintain the report and additional files. Keep original run artifacts under `runs/`. Before deleting a referenced checkout, preserve its required code and update the links; retain cited evidence.

## Keep team and session records

The lead keeps the check schedule in `<output-dir>/team/README.md` and links each Observer note under `team/observations/`. Existing task records hold action owners, decisions and expected results. Keep unresolved findings linked to their first note, update their progress there, and have the report link to this current state. Each observation preserves the evidence and judgment from that check.

### Read session evidence

Start from the task and experiment directory to find the lead and member sessions. Use working directories, times, session IDs and parent/child relationships to identify the relevant records. The Observer finds these itself and reads them without asking members to report progress.

Use a read-only history interface with full items and tool output when available: [Codex thread history](https://learn.chatgpt.com/docs/app-server) or [Claude session interfaces](https://code.claude.com/docs/en/agent-sdk/sessions). Original files are commonly stored in:

- Claude Code: `~/.claude/projects/`, including session `subagents/` directories.
- Codex: `$CODEX_HOME/sessions/`, with `CODEX_HOME` defaulting to `~/.codex`.

For original JSONL files, the [session reader](../.agents/skills/rsi/scripts/session_records.py) provides a paged index and reads related evidence together. It requires Python 3.11+ with no additional packages. Set `RSI_SESSION` to the file and `RSI_SINCE` to an ISO start time with a UTC offset, then run from the checkout root:

```bash
python -I .agents/skills/rsi/scripts/session_records.py index "$RSI_SESSION" --since "$RSI_SINCE"
```

The index includes Codex start, completion and interruption events for checking member activity over time. It omits identifiable inherited records and reports their count; add `--include-inherited` when that background is needed. Continue with `--after-line` and its returned `next_after_line`. Choose a source line, set `RSI_LINE`, and read the evidence:

```bash
python -I .agents/skills/rsi/scripts/session_records.py show "$RSI_SESSION" "$RSI_LINE"
```

`show` includes the selected record, its paired tool input and result, and command records within that interval. Each keeps its source location; interval membership alone does not establish which call caused a command. For long output, pass `next_offset` to `--offset` and keep the first response's `through_line` in `--through-line` on every subsequent page. This keeps the evidence range stable while the session grows. Start a new read without these options to include newer records.

The reader marks identifiable inherited history, missing results, unavailable content and stored truncation markers. Read saved-output or artifact files when the session lacks the needed detail; otherwise leave the fact unresolved. Index previews locate evidence but do not replace it.

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
| `iterations/<name>/notes/iteration.md` | The assigned Experimenter maintains the summary and document index, and appends run findings, independent reviews, decisions, and corrections with timestamps, sources, and evidence links. |
| Other files under `iterations/<name>/notes/` | Each author maintains the full proposal, plan, analysis or review linked from the iteration notes. |
| `report.md` | The designated writer updates overall comparisons, the retained approach and open questions, linking the relevant iteration entries and decisions. |

Attribute analysis, reviews, and decisions to their authors. Each decision states the action and its scope. When correcting a conclusion or changing a decision, link the earlier entry and new evidence so readers can follow how the understanding changed.

After each run, append its directory or W&B link, the hypothesis tested, the comparison artifact when available, and the supported conclusion. Record failures and interruptions as well. Use the [iteration template](templates/iteration-template.md) for the history. Send contributions to the assigned writer so parallel agents do not overwrite one another's entries.

Keep the report's current conclusions aligned with the iteration history and link the supporting entries. Reusable knowledge can also be revised as understanding improves, with links back to the experiment history.

## Maintain the user-facing report

At experiment start, the lead or a designated writer creates `report.md` using the [report template](templates/report-template.md) and shares its path with the user. Use the task's delivery location, or the experiment root when none is specified. Link the task, experiment directory and iteration notes from the report, resolving links from its actual location. Keep the same report when resuming. The same writer maintains the report and its [progress chart data and figures](templates/progress-charts.md); benchmark commands save the underlying evidence.

Update the report after each measured iteration, including failed attempts, and whenever analysis changes a conclusion or the retained approach. Follow the template's summary tables and progress charts with an explanation of each approach's motivation, actual implementation and results in the same report. The summary and approach sections use matching descriptive names and links. Keep complete commands, logs, repeated measurements and decision history in the iteration notes.

Write for readers who did not participate in the experiment, separating supported conclusions from hypotheses and pending validation. Before measurements are available, state the objective, starting conditions and what remains unmeasured.

Every report update includes updating the chart data, regenerating both figures and viewing them to check the result.

On pause, stop or completion, update the same report with the reason, results obtained, retained changes and unfinished validation. Replace obsolete plans with the actual state and share the report path again.
