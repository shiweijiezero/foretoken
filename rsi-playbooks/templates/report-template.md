# Optimization Report

English | [简体中文](report-template_zh.md)

Use this for the task's continuously maintained `report.md`, following the [report workflow](../experiment-records.md#maintain-the-user-facing-report). Deliver a file by default; keep the report updated on a document platform when the user specifies one. Begin with the experiment's objective, current status and last update time. For a time-bounded task, include its original start and deadline. Link the task and experiment result directory.

## Summary

State what was optimized, the measured improvement and the retained approach in a few sentences. Identify the model and workloads.

Use one overview table per task type, such as random requests, multi-turn conversations or answer quality, with its iteration chart immediately above it. Keep them adjacent. Include the initial baseline, retained changes, regressions, rejected approaches and failed attempts so readers can see the complete search.

Each row summarizes one approach; each point represents that approach's result for the plotted metric and condition. Use the same names, values and ordering. Name rows as an ablation table: `baseline`, `+ A`, `+ A + B`, listing the complete measured changes from the original baseline in each name. Summarize comparable repeated runs with their count and variability, linking the individual measurements. Keep different conditions separate in table cells and chart panels or series. Count approaches separately from measurement runs.

The user's initial configuration is the only baseline and stays first across sessions. Later retests and local controls are named references, not replacement baselines. Every baseline ratio uses the original comparable measurement; where none exists, show absolute values and leave that ratio unmeasured. Use a short conditions column for changed hardware, software or workload protocols; keep local-control comparisons in the linked details. Show concurrency levels side by side and spell them out, such as “Concurrency 1” and “Concurrency 16”. Run IDs, full configurations and logs belong in linked records.

Keep chart titles short: model, dataset and metric are enough. Mark retained milestones and failed attempts; ordinary measured points need no “valid” label. The main chart shows all summarized approaches. Additional milestone-only or per-condition figures belong with the details. See the [plotting guide](progress-charts.md).

### Task type

Adapt the following layout to the task's metrics. Values include their change from the stated reference; “—” means unmeasured or undefined, with a reason in the decision column. State the statistic and repeat count. Quality tables retain evaluator sample counts and error estimates, and identify ordinary evaluations, diagnostics and tool validation by purpose.

![Approach iteration history](figures/progress-search.png)

| Approach | Conditions | Concurrency 1: tokens/s ↑ | Concurrency 1: ms/token ↓ | Concurrency 16: tokens/s ↑ | Concurrency 16: ms/token ↓ | Repeats | Decision |
| --- | --- | ---: | ---: | ---: | ---: | --- | --- |
| baseline | Initial conditions | Value (—) | Value (—) | Value (—) | Value (—) | Count | Reference |
| [+ Finer prefix matching](#finer-prefix-matching) | Same conditions | Value (%) | Value (%) | Value (%) | Value (%) | Count | Retained / rejected / under investigation |

Link the chart data and [original results](../experiment-records.md#analyze-results). Put any client, background-build or output-length difference that changes the interpretation in the affected row. Explain mechanisms and trade-offs in the approach details without repeating the table's values.

## Approach details

Explain each approach in the report using the same names as the summary. Related parameter variants can share a section when their changes and outcomes are identified separately. The following section shows the structure to repeat.

### Finer prefix matching

Motivation: describe the bottleneck or observation that prompted the approach, the expected benefit and the hypothesis being tested.

Implementation: identify the actual code or configuration changes and the reference approach they build on. Explain which changes are combined and how they affect execution.

Results: compare the measurements with that reference, explain the observed gains or regressions, and state whether the approach was retained, rejected or awaits measurement. Distinguish the measured result from its proposed explanation.

Link the corresponding `iterations/<name>/notes/iteration.md`, code or configuration, and original results. Keep complete commands, logs, repeated measurements and review history in those records; the report contains the explanation readers need to understand the approach. Resolve links from the report's location.

## Execution and delivery

Summarize time and resource use as required by the task, including the observation interval and sampling coverage. Link the retained configuration, code and key figures, and identify remaining validation or research questions. State when no approach has been selected. When the experiment pauses or ends, record the reason and actual resource state.
