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

Authors maintain `notes/`. Commands, source revisions, file snapshots, timestamps, and execution status belong in automatically generated run records. Benchmark configurations, metrics, logs, and profiles belong with the run artifacts. Link those files from the notes rather than maintaining a second copy of their fields.

Choose only the workload needed to answer the current question. Record time spent researching, editing, preparing the environment, and analyzing results; label estimates and link measured durations.
