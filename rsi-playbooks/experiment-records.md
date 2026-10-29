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
├── report.md                  # Task overview linking generated results
├── figures/
│   └── progress/
│       ├── progress-summary.md     # Generated comparisons and figures
│       ├── progress-summary.csv
│       └── progress-runs.csv
├── team/                      # Agent-maintained team and assessment notes
│   ├── README.md              # Task, reminders and follow-up decisions
│   └── evaluations/          # One dated note per Evaluator check
└── iterations/tp2/
    ├── notes/
    │   ├── iteration.md       # Owner analysis, document links and decisions
    │   ├── progress.yaml      # Run references and scope-specific judgments
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

Keep each approach's question, hypothesis, results and decisions in `<output-dir>/iterations/<name>/notes/iteration.md`. A short experiment needs only this note and links to its original results. Reuse the approach name with `--iteration` when running follow-up measurements.

Split out a longer proposal, analysis or independently authored review only when it needs its own document, then link it from `iteration.md`. Keep dated findings and decisions in the note. `report.md` states the task goal and links the generated comparisons and these records.

The lead provides all members with a readable experiment directory, its host and absolute path. Use that absolute `--output-dir` across checkouts sharing a filesystem, with `--iteration` distinguishing candidates. For records on hosts without shared storage, link their actual locations and access instructions from the report.

Code stays in its assigned checkout or worktree. The proposal names its owner, host, absolute code path, branch and revision, links relevant files or a saved diff, and notes any uncommitted changes.

Follow the [team's communication guidance](../.agents/skills/rsi/roles/common.md#share-findings-and-keep-working): brief coordination uses messages, while long explanations and material for several readers go in shared files. Send the summary and exact path, then describe later changes without repeating all the background. Discuss open choices directly with the person responsible, record decisions in the notes, and continue other work while waiting for replies. Each document has one writer; save substantial reviews or alternative proposals in the author's own files.

For example, a throughput improvement points to the reference and candidate `metrics.json` files and the compared fields. An error points to the log file and relevant lines or time range; a code finding identifies the revision, file and function. Findings about agent decisions or tool calls point to the session record and message or time range. Send the finding with these source locations so recipients can read the relevant portions first, expanding the context when needed.

Supporting research, scripts, preparation logs and cleanup records also belong under the iteration's `notes/`, organized as needed. Filenames such as `plan.md` and `analysis.md` are examples. Benchmark commands create missing iteration notes without replacing existing content; owners maintain their analysis and progress records. Keep original run artifacts under `runs/`. Before deleting a referenced checkout, preserve its required code and update the links; retain cited evidence.

## Keep team and session records

The check schedule belongs in `<output-dir>/team/README.md`; the Evaluator stores its dated findings under `team/evaluations/`. Existing task records hold action owners, decisions and expected results. Keep unresolved findings linked to their first note, update their progress there, and have the report link to this current state. Each assessment preserves the evidence and judgment from that check.

Task records describe intent, ownership and decisions. Read actual agent activity from the native harness, hook bindings from continuation status, and jobs from their execution owner. When a report needs a snapshot, include its observation time and source. Avoid maintaining another live-state copy in each task, member and report file. Close an action with its owner's link to the resulting change or explanation of the decision to stop.

For autonomous continuation, the Agent Scheduler is the owner of native wake-up. It is bound to one RSI root session and does not create or replace the Evaluator timetable. `members.responsibility` is the durable full role brief, `thread_id` is the native app-server identity, and `dispatch_state` records `pending`, `dispatching` or `running` with a lease and accepted turn ID. Hooks may record a pending transition, but they never claim that a queued item woke a thread. The scheduler stops on explicit pause, completion, expiry or deadline and leaves the durable assignment for recovery.

If `CODEX_APP_SERVER_SOCKET` is omitted, the scheduler starts a dedicated stdio app-server. Use an existing control socket only when the host explicitly owns that daemon.

### Read session evidence

Start from the task and experiment directory to find the lead and member sessions. Use working directories, times, session IDs and parent/child relationships to identify the relevant records. The Evaluator finds these itself and reads them without asking members to report progress.

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
| `iterations/<name>/notes/progress.yaml` | The Experimenter records selected runs, measured parent and judgments for each scope. The plot command derives the shared comparisons from these records. |
| `members.responsibility` / `members.thread_id` | The complete continuing role brief and native app-server identity used by the Agent Scheduler; these are not replaced by one queued stage. |
| `report.md` | The concise user-facing overview owns task metadata, status and links. One designated overview owner updates its prose under `.foretoken-report.lock`; numerical comparisons come from the generated progress summary. |

Attribute analysis, reviews, and decisions to their authors. Each decision states the action and its scope. When correcting a conclusion or changing a decision, link the earlier entry and new evidence so readers can follow how the understanding changed.

After each run, the Experimenter adds its original location and finding to the [iteration notes](templates/iteration-template.md), then directly resumes the Worker and any assigned reviewer using the [team workflow](../.agents/skills/rsi/roles/common.md#return-results-as-work-finishes). For example, a diagnostic command can finish with exit 0 while its saved output comparison fails. Record both facts and link the comparison; distinguish measured speed, applicable validation evidence and the retention decision.

Include formal measurements, diagnostic controls and tool-validation runs with their purposes and sample counts. Link auxiliary artifacts even when they have no `metrics.json`. On remote runs, the Experimenter preserves the agreed result location, including any transfer from scratch storage. Recipients read the originals there or use the delivered copy. On recovery, check the recorded remote job and result location.

Keep the report's current conclusions aligned with the iteration history and link the supporting entries. Reusable knowledge can also be revised as understanding improves, with links back to the experiment history.

## Maintain the user-facing report

At task setup, create `report.md` using the [report template](templates/report-template.md), link the task and result directory, and share its path. Keep the same report when resuming. Assign one continuing member as the overview owner; this owner updates only metadata, status, links and short approach context under the task-local `.foretoken-report.lock`. The owner is not a measurement or dispatch bottleneck.

After a measured stage or a changed judgment, update `iterations/<name>/notes/progress.yaml` and run:

```bash
foretoken plot --progress results/decode/parallelism
```

The command reads every iteration's selected saved runs and judgments, then generates `figures/progress/progress-summary.md`, matching CSV tables and the native milestone/search charts. The generated summary is the numerical source of truth; `report.md` links it instead of copying a cross-scope table. Each scope starts with measured retained branch tips and actual run/configuration/source links for choosing an applicable next parent; adopted ancestors remain in the history. Metrics, repetition counts, variability and recorded code/configuration evidence come from the existing run records. Detailed mechanisms, output differences and independent reviews stay in the owner's `iteration.md` and linked analyses.

For example, a candidate's progress record can select one completed command and its measured parent:

```yaml
label: + QKV views
measurements:
  random-concurrency-1:
    runs: [../runs/perf-000001]
    parent: ../../baseline/runs/perf-000001
    status: valid
    retained: true
```

Paths start from this YAML's directory. A scope names a workload and comparison condition; keep different protocols and conditions separate. Select only comparable repetitions in each scope. Mark the user's initial configuration with `baseline: true` in that iteration's record; later controls remain ordinary named approaches. Use `reference` while an applicable output or quality question remains unresolved, and record rejected or failed approaches as well. The [progress guide](templates/progress-charts.md) defines the fields and custom metric selection.

Continue implementation and exploration from the current validated configuration for the relevant scope. Record the actual parent that the candidate ran, including its existing changes. Independently measured A and B from P are two branches; adopting either does not establish A+B. A combined configuration needs its own run and judgment. Use the smallest execution that verifies the changed boundary; reuse applicable evidence rather than routinely repeating full matrices or paired controls.

Each iteration has one Experimenter publishing its progress record through the [atomic update example](templates/progress-charts.md#refresh-the-shared-view), then refreshing the shared view directly. A progress file has one writer and is replaced atomically; the plot command serializes discovery, reading and publication under the existing figure-directory lock. If several progress files change during a redraw, rerun after the owners finish so the summary records one settled snapshot. The generated tables retain each measured parent and actual run/source references. Milestones connect only recorded measured parents, leaving independent branches distinct. Retention is an explicit scope-specific judgment, never a choice made from maximum throughput.

For a document platform, publish or link the same generated summary rather than maintaining another numerical report. To change the overview, run the update command under the report lock, for example:

```bash
python3 -I .agents/skills/rsi/scripts/report_lock.py \
  --report results/decode/parallelism/report.md -- \
  "${EDITOR:-vi}" results/decode/parallelism/report.md
```

The editor opens the current file only after acquiring the lock. A scripted updater must likewise reread the current report inside `report_lock()` and write a complete replacement; it must not append a stale copy. On pause, stop or completion, record the reason and remaining work in the task overview and link the owners' actual cleanup evidence. Existing manually authored chart YAML remains supported by `foretoken plot --progress CHART.yaml`.
