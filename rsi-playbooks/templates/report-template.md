# Optimization Report

English | [简体中文](report-template_zh.md)

Use this for the task's continuously maintained `report.md`, following the [report workflow](../experiment-records.md#maintain-the-user-facing-report). Deliver a file by default; keep the report updated on a document platform when the user specifies one. Begin with the experiment's objective, current status and last update time. For a time-bounded task, include its original start and deadline. Link the task and experiment result directory.

## Summary

State the retained approach, measured gains, quality trade-offs and unfinished validation. Briefly identify the model, software version, hardware and precision so readers can place the results.

Place the comparison tables in this summary, one per task type, such as random requests, multi-turn conversations or answer quality. Start with `baseline`, then give each approach its own row. Use short names that describe the change, such as "Finer prefix matching" or "Larger token batches"; name combined changes explicitly. Link each name to its explanation below.

Choose columns for the task's conditions and metrics, with units and improvement directions. State workload and concurrency beside the table. Retain the initial configuration as the task's baseline across sessions and time windows. When hardware, workload or measurement conditions change, group those rows with their applicable controls; calculate changes only within comparable groups. Label later control measurements and explain which one each percentage uses.

### Task type

Replace this heading and the metric columns with those of the actual task. The rows illustrate the layout; fill them with measured results.

| Approach | Metric A (unit, direction) | Change from baseline | Metric B (unit, direction) | Change from baseline |
| --- | ---: | ---: | ---: | ---: |
| baseline | Measured value | — | Measured value | — |
| [Finer prefix matching](#finer-prefix-matching) | Measured value | Percentage change | Measured value | Percentage change |

State the repeat count `n` and statistic used. Show a single value for one measurement; for repeats, include the mean and variability or link their detailed values. Keep regressions and use “—” for unmeasured or undefined entries, with a reason. Quality scores retain the evaluator's sample counts and error estimates. Source results follow the [experiment records guide](../experiment-records.md#analyze-results).

Alongside the values, state whether each approach is retained, rejected or still under investigation and link the relevant interpretation. Separate ordinary quality evaluations from instrumented diagnostics and tool validation. Mention comparison differences where they affect a row, such as changed clients, background builds or output lengths.

## Optimization progress

Generate the [progress charts](progress-charts.md), then insert these references for each metric and comparison condition, adjusting filenames as needed:

```markdown
![Measured optimization milestones](figures/progress-milestones.png)

![Optimization search history](figures/progress-search.png)

[Chart data](figures/progress.yaml)
```

## Approach details

Explain each approach in the report using the same names as the summary. Related parameter variants can share a section when their changes and outcomes are identified separately. The following section shows the structure to repeat.

### Finer prefix matching

Motivation: describe the bottleneck or observation that prompted the approach, the expected benefit and the hypothesis being tested.

Implementation: identify the actual code or configuration changes and the reference approach they build on. Explain which changes are combined and how they affect execution.

Results: compare the measurements with that reference, explain the observed gains or regressions, and state whether the approach was retained, rejected or awaits measurement. Distinguish the measured result from its proposed explanation.

Link the corresponding `iterations/<name>/notes/iteration.md`, code or configuration, and original results. Keep complete commands, logs, repeated measurements and review history in those records; the report contains the explanation readers need to understand the approach. Resolve links from the report's location.

## Execution and delivery

Summarize time and resource use as required by the task, including the observation interval and sampling coverage. Link the retained configuration, code and key figures, and identify remaining validation or research questions. State when no approach has been selected. When the experiment pauses or ends, record the reason and actual resource state.
