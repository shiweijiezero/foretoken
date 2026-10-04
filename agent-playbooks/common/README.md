# Common Guidance

Shared principles, prompts, experiment recording rules, and evidence requirements for all playbooks.

Use [`experiment-template.md`](experiment-template.md) for the stable description of an experiment, and [`iteration-template.md`](iteration-template.md) for each iteration. Store them under:

```text
results/<experiment-purpose>/
├── experiment.md
└── iterations/<sequence>-<short-name>/
    ├── record.md
    ├── context.json
    ├── changes/
    └── runs/
```

Keep generated benchmark outputs, logs, profiles, and W&B runs in or linked from the corresponding iteration directory. The `changes/` directory mirrors the repository paths changed for that iteration.
