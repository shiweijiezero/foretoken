# Experiment Records

English | [简体中文](experiments_zh.md)

Keep related approaches and measurements together so that later iterations can build on earlier findings. An experiment describes an overall goal; an iteration explores an approach; a run is one performance or quality evaluation command.

## Organize the records

Store an experiment under `results/<goal>/<motivation>/`. Give each iteration a name that describes its approach, such as `queue-aware-routing`.

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


Update the experiment notes with overall findings and the iteration notes with the current hypothesis, design, changes, and interpretation. Refer to the [experiment](../templates/experiment-template.md) and [iteration](../templates/iteration-template.md) templates as needed.

Use `--output experiment`, `--output-dir results/<goal>/<motivation>`, and `--iteration <name>` to group perf/eval commands under one iteration. Repeated commands create separate runs without overwriting earlier results.

With this output selected, `generated/` holds captured commands, timing, status, and available source snapshots; `artifacts/` holds evaluation configurations and results. Notes are maintained by the developer or agent and link to these records. Run from the checkout containing the changes to capture its source state; record the source of separately built serving code in the notes.

## Explain the results

State which hypothesis the measurements support or contradict, and what remains unresolved. Account for differences in workload, precision, cache state, or hardware when comparing results. Link the measurements and model outputs used to reach the conclusion.

An optional final review can inspect the implementation and raw outputs for benchmark-specific shortcuts, changes in model behavior, and unsupported attribution of gains.

## Track iteration time

Record time spent on investigation, design, implementation, deployment, evaluation, and analysis. Use measured durations where available and label estimates. Keep command duration distinct from its measurement window, and avoid adding nested durations together.

Use this breakdown to decide what to reuse or streamline in the next iteration, and choose only the evaluations needed to answer its question.
