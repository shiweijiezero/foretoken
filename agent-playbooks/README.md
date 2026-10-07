# Inference Optimization Playbooks

English | [简体中文](README_zh.md)

We want agents to work autonomously and independently over sustained periods with Foretoken, improving inference performance, resource efficiency, and service quality.

We provide these playbooks to help developers and agents find references, conduct optimization experiments, and record iteration results. Developers can use them directly or to guide an agent. Methods and findings from experiments can then become part of the shared guidance for future work.

## Design goals

- Rapid deployment: apply code changes quickly to inference services in the target cluster.
- Testing and feedback: measure performance, model quality, and resource costs to evaluate the effects of a change.
- Observation and diagnosis: use logs, metrics, and profiling to locate runtime problems and performance bottlenecks.
- Experiment records: connect each iteration's changes, configurations, results, and decisions for comparison and reproduction.
- References and shared experience: find and update references, and turn experimental findings into reusable guidance.
- Behavior and attribution checks: examine actual model behavior, identify metric gaming, and determine whether gains come from the intended change.

## Directory structure

| Directory | Purpose |
| --- | --- |
| [templates/](templates/README.md) | Reference templates for experiments, iterations, and technique notes |
| [guidance/](guidance/README.md) | Reference material and knowledge for inference-system optimization |
| [tasks/](tasks/README.md) | Goals, environments, and scope for common optimization tasks |

## Steps

1. Define the behavior to improve and how to judge success, such as reducing time to first token under a given workload while preserving answer quality.
2. Set the scope: focus on routing, the inference engine, or another specific area, or leave it open to changes across the whole system.
3. Read the code and relevant research and practice. Use existing measurements to understand the system's current behavior, taking additional measurements as needed.
4. Analyze the causes and opportunities for improvement, develop your own hypotheses, and design an approach. State the expected effects and choose workloads and metrics that test those hypotheses. If comparable results are unavailable, measure the service before making the change.
5. Change the code or configuration and update the inference service through the [source deployment workflow](../docs/custom-deployment.md).
6. Run the selected [evaluations](../benchmarks/README.md) and compare results before and after the change. Use model outputs, logs, and profiling to determine what accounts for the differences.
7. After each evaluation, the developer or agent adds the run link, changes, result interpretation, and time spent to `results/<goal>/<motivation>/iterations/<name>/notes/iteration.md`. At the end of the iteration, update the experiment root’s `notes/experiment.md`. Refer to the [note templates](templates/README.md) and [record layout](#organize-the-records).
8. Keep, refine, or revert the change, then choose the next question to investigate. Add reusable findings to the guidance and references.
9. Optional: review the implementation and raw results for benchmark-specific shortcuts, changes in model behavior, or differences in comparison conditions, and check whether the claimed gains are attributable to the intended change.

## Choose evaluations

Select evaluations for the current optimization goal. Use the [experiment command reference](../benchmarks/docs/recipes.md), replacing its output options with the `experiment` settings described below.

| Evaluation goal | Tools and commands | Results to inspect |
| --- | --- | --- |
| Compare latency, decode speed, and throughput | [Performance evaluation](../benchmarks/docs/perf/README.md), `foretoken perf` | Success count, latency distribution, TPOT, throughput, and actual output length |
| Evaluate answer quality | [Quality evaluation](../benchmarks/docs/eval/README.md), `foretoken eval` | Scores, sample counts, scoring method, and individual answers |
| Locate computation, communication, and waiting costs | [Profiling](../benchmarks/docs/profile/README.md), `foretoken perf --profile` | Execution timeline; use separate unprofiled runs for speed comparisons |
| Compare concurrency, input lengths, and traffic patterns | [Sweep and workload configurations](../benchmarks/docs/recipes.md) | Metrics and trends across workload points |

Inspect command options with `foretoken perf --help` and `foretoken eval --help`.

## Organize the records

An experiment describes an overall goal; an iteration explores an approach; a run is one performance or quality evaluation command. Developers or agents write notes under `results/`, while commands save execution evidence automatically.

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

`generated/` holds run information; `artifacts/` holds benchmark results. With `quiet` enabled, execution logs are saved to `generated/run.log`.

Run `foretoken perf` or `foretoken eval` with `--output experiment`, `--output-dir results/<goal>/<motivation>`, and `--iteration <name>`. Replace the placeholders with the chosen goal, motivation, and iteration name. Reuse these options for the same approach; each command adds a run directory. Without `--iteration`, each command creates a new numbered iteration.

The command creates blank note templates only when they do not exist. Developers or agents fill them in; subsequent runs do not overwrite the notes.

### Record code changes

Run evaluations with `--output experiment` from the source checkout. The command records the current Git commit ID and copies modified or newly added, uncommitted files so you can inspect the code changes for this experiment later:

- `generated/context.json`: commit ID and change list.
- `generated/changes/`: copies of changed files at their original paths, excluding Git-ignored files.

If the service uses a separately built image or another checkout, identify its source in the iteration notes. Source records and author-written notes stay local.

## Inspect the results

Find the run under `iterations/<name>/runs/`. The benchmark result directory printed by the runner is inside its `artifacts/`; a sweep command may contain several result directories.

| Order | File | What to check |
| --- | --- | --- |
| 1 | `generated/context.json` | Completed, failed, or interrupted status; exit code, command, and source capture status |
| 2 | `config.json` and `environment.json` in the result directory | Whether workloads, generation settings, and client and serving environments are comparable |
| 3 | `metrics.json` in the result directory | Performance metrics or quality scores, together with success counts, sample counts, and scoring methods |
| 4 | Performance `raw_output.json`, or quality `native/` | Raw responses, individual answers, and completion status to check the behavior behind aggregate metrics |
| 5 | `generated/run.log` (with `quiet`), benchmark logs, or `evaluator.log` | Failure causes, preparation, and actual execution |

For per-sample quality records, add `--log_samples` with lm-evaluation-harness. See [performance results](../benchmarks/docs/perf/wandb.md) for resource and serving metric charts; log in before selecting W&B output.

To redraw saved results, set `RESULT_DIR` to the concrete result directory printed by the runner:

```bash
foretoken plot "$RESULT_DIR" --columns 2
```

This does not rerun inference. If a profile was captured, use `foretoken profile view` following the [viewing guide](../benchmarks/docs/profile/README.md) to open the timeline. After inspecting results, write the notes described below.

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
