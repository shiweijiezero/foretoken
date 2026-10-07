# Experiment Records

English | [简体中文](experiments_zh.md)

Keep related approaches and measurements together so that later iterations can build on earlier findings. An experiment describes an overall goal; an iteration explores an approach; a run is one performance or quality evaluation command.

## Choose evaluations

Choose evaluations for the current question rather than running every suite. Use the [experiment command reference](../../benchmarks/docs/recipes.md), replacing its output options with the `experiment` settings described below.

| Question | Entry point | What to inspect |
| --- | --- | --- |
| Did latency, decode speed, or throughput improve? | [Performance evaluation](../../benchmarks/docs/perf/README.md), `foretoken perf` | Success count, latency distribution, TPOT, throughput, and actual output length |
| Did answer quality change? | [Quality evaluation](../../benchmarks/docs/eval/README.md), `foretoken eval` | Scores, sample counts, scoring method, and individual answers |
| Is time spent computing, communicating, or waiting? | [Profiling](../../benchmarks/docs/profile/README.md), `foretoken perf --profile` | Execution timeline; use separate unprofiled runs for speed comparisons |
| What changes with concurrency, input length, or traffic? | [Sweep and workload configurations](../../benchmarks/docs/recipes.md) | Results at each workload point rather than only an overall average |

For decode optimization, follow [Qwen decode optimization](../tasks/qwen-decode.md). Inspect command options with `foretoken perf --help` and `foretoken eval --help`.

## Organize the records

Choose an experiment directory under `results/<goal>/<motivation>/` and a name for the current approach, such as `queue-aware-routing`. The directory layout below contains two performance runs and one quality evaluation for that approach, plus the location for another approach.

```text
results/<goal>/<motivation>/
├── notes/
│   └── experiment.md
└── iterations/
    ├── queue-aware-routing/
    │   ├── notes/
    │   │   └── iteration.md
    │   └── runs/
    │       ├── perf-000001/
    │       │   ├── generated/
    │       │   │   ├── context.json
    │       │   │   ├── changes/
    │       │   │   └── run.log
    │       │   └── artifacts/
    │       │       └── <result-directory>/
    │       │           ├── config.json
    │       │           ├── environment.json
    │       │           ├── metrics.json
    │       │           └── ...
    │       ├── eval-000001/
    │       │   ├── generated/
    │       │   └── artifacts/
    │       └── perf-000002/
    │           ├── generated/
    │           └── artifacts/
    └── another-approach/
        ├── notes/
        │   └── iteration.md
        └── runs/
```

This layout is produced by `--output experiment`. `context.json` records the command, status, timing, and source information. When source capture is available, `changes/` retains modified files at their repository-relative paths. `run.log` is saved with `quiet` enabled. `artifacts/` preserves the benchmark tool's result directories and files.

Run `foretoken perf` or `foretoken eval` with `--output experiment`, `--output-dir results/<goal>/<motivation>`, and `--iteration <name>`. Replace the placeholders with the chosen goal, motivation, and iteration name. Reuse these options for the same approach; each command adds a run directory. Without `--iteration`, each command creates a new numbered iteration.

The command creates blank note templates only when they do not exist. Developers or agents fill them in; subsequent runs do not overwrite the notes. Run from the checkout containing the changes to capture its source state; record the source of separately built serving code in the notes.

## Inspect the results

Find the run under `iterations/<name>/runs/`. The benchmark result directory printed by the runner is inside its `artifacts/`; a sweep command may contain several result directories.

| Order | File | What to check |
| --- | --- | --- |
| 1 | `generated/context.json` | Completed, failed, or interrupted status; exit code, command, and source capture status |
| 2 | `config.json` and `environment.json` in the result directory | Whether workloads, generation settings, and client and serving environments are comparable |
| 3 | `metrics.json` in the result directory | Performance metrics or quality scores, together with success counts, sample counts, and scoring methods |
| 4 | Performance `raw_output.json`, or quality `native/` | Raw responses, individual answers, and completion status to check the behavior behind aggregate metrics |
| 5 | `generated/run.log` (with `quiet`), benchmark logs, or `evaluator.log` | Failure causes, preparation, and actual execution |

For per-sample quality records, add `--log_samples` with lm-evaluation-harness. See [performance results](../../benchmarks/docs/perf/wandb.md) for resource and serving metric charts; log in before selecting W&B output.

To redraw saved results, set `RESULT_DIR` to the concrete result directory printed by the runner:

```bash
foretoken plot "$RESULT_DIR" --columns 2
```

This does not rerun inference. If a profile was captured, use `foretoken profile view` following the [viewing guide](../../benchmarks/docs/profile/README.md) to open the timeline. After inspecting results, write the notes described below.

## Add notes after each run

After the command finishes, open `iterations/<name>/notes/iteration.md`. The developer or agent appends an explanation of the run, including:

- A link to the run and the change or hypothesis it tested.
- What differed from the reference result, whether model outputs met expectations, and what the evidence supports.
- The cause and next action for a failure, interruption, or inconclusive result, plus time spent on work not recorded by the command.

Keep earlier explanations when adding runs, identifying each by its run name. Leave raw metrics and logs in their run directories and link to them from the notes.

## At the end of each iteration

1. Summarize the runs in `iterations/<name>/notes/iteration.md`: whether they support the hypothesis, whether workload, precision, cache, or hardware differences affect the conclusion, and what remains unresolved. Optionally review the implementation and raw outputs for benchmark-specific shortcuts and changes in model behavior.
2. Decide whether to retain, refine, or revert the iteration's changes. Record the resulting code and deployment state and the next question. Revert only changes made for this iteration.
3. Update the experiment root's `notes/experiment.md` with new or revised findings and a link to the iteration note. Preserve the original run artifacts.
4. When findings are reusable, add the knowledge and applicable conditions to `guidance/`, or task-specific methods to `tasks/`, citing evidence.
5. Check ownership and active use before cleaning up temporary environments, background tasks, and caches no longer needed. Preserve experiment records and resources needed by the next iteration, noting why those resources are retained.

Additional runs of the same approach update the existing iteration note. Use a new iteration name for a different approach.

## Track iteration time

In the iteration note, record time spent on investigation, design, implementation, deployment, evaluation, and analysis separately. Use measured durations where available and label estimates. Keep command duration distinct from its measurement window, and avoid adding nested durations together.

Use this breakdown to identify work that can be reused or streamlined in the next iteration.
