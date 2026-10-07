# Experiment Records

English | [简体中文](experiments_zh.md)

Keep related approaches and measurements together so that later iterations can build on earlier findings. An experiment describes an overall goal; an iteration explores an approach; a run is one performance or quality evaluation command.

## Organize the records

Choose an experiment directory under `results/<goal>/<motivation>/` and a name for the current approach, such as `queue-aware-routing`. The example below shows two performance runs and one quality evaluation for that approach, plus the location for another approach.

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

## At the end of each iteration

1. Inspect each run's exit status, metrics, and model outputs. Link the records from `iterations/<name>/notes/iteration.md`; explain failed or interrupted runs and identify the evidence still available.
2. Update that iteration note with the actual changes, differences from the reference results, whether the hypothesis was supported, and time spent investigating, designing, deploying, evaluating, and analyzing. If the evidence is insufficient, name the missing measurement.
3. State and carry out the decision to retain, refine, or revert the iteration's changes. Record the resulting code and deployment state and the next question to test. Revert only changes made for this iteration.
4. Update the experiment's `notes/experiment.md` with new or revised findings and a link to the iteration note. Preserve earlier run artifacts.
5. When a finding is reusable, add the method and its applicable conditions to `guidance/`, or update the relevant `tasks/` playbook for task-specific procedures. Cite the supporting evidence.
6. Clean up temporary environments, background tasks, and caches no longer needed by the iteration. Check ownership and active use first. Preserve results, source records, and resources needed by the next iteration, and state why retained resources are still needed in the iteration note.

Additional runs of the same approach update the same iteration note and add run records. Use a new iteration name when starting a different approach.

## Explain the results

State which hypothesis the measurements support or contradict, and what remains unresolved. Account for differences in workload, precision, cache state, or hardware when comparing results. Link the measurements and model outputs used to reach the conclusion.

An optional final review can inspect the implementation and raw outputs for benchmark-specific shortcuts, changes in model behavior, and unsupported attribution of gains.

## Track iteration time

In the iteration note, record time spent on investigation, design, implementation, deployment, evaluation, and analysis separately. Use measured durations where available and label estimates. Keep command duration distinct from its measurement window, and avoid adding nested durations together.

Use this breakdown to identify work that can be reused or streamlined in the next iteration.
