# Common Guidance

English | [简体中文](README_zh.md)

Use the [experiment template](experiment-template.md) to describe the goal and comparison, and the [iteration template](iteration-template.md) to explain each approach and its outcome.

```text
results/<goal>/<motivation>/
├── notes/experiment.md
└── iterations/<name>/
    ├── notes/iteration.md
    └── runs/<run>/
        ├── generated/
        └── artifacts/
```

Authors maintain `notes/`. Commands, source snapshots, timestamps and status are recorded under `generated/`; benchmark configurations, metrics and profiles belong under `artifacts/`. Link these files from the notes rather than copying their fields.

Choose only the workload needed to answer the current question. Record time spent researching, editing, preparing the environment, and analyzing results; label estimates and link measured durations.
