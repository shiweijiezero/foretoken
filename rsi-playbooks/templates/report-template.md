# Optimization Report

English | [简体中文](report-template_zh.md)

Use this for the task's continuously maintained `report.md`, following the [report workflow](../experiment-records.md#maintain-the-user-facing-report). Deliver a file by default; keep the report updated on a document platform when the user specifies one. Begin with the experiment's objective, current status and last update time. For a time-bounded task, include its original start and deadline. Link the task and experiment result directory.

## Summary

State what was optimized, the measured improvement and the retained approach in a few sentences. Identify the model and workloads.

Use one overview table per task type, such as random requests, multi-turn conversations or answer quality, with its iteration chart immediately above it. Keep them adjacent. Include the initial baseline, retained changes, regressions, rejected approaches and failed attempts so readers can see the complete search.

Each row summarizes one approach; each point represents that approach's result for the plotted metric and condition. Use the same names, values and ordering. Name rows as an ablation table: `baseline`, `+ A`, `+ A + B`, listing the complete measured changes from the original baseline in each name. Summarize comparable repeated runs with their count and variability, linking the individual measurements. Distinguish different workload protocols in the approach names and chart panels. Count approaches separately from measurement runs.

The user's initial configuration is the only baseline and stays first across sessions. Later retests and local controls are named references, not replacement baselines. Show the signed percentage change from the original baseline alongside every measured metric. When the workload protocol has no initial measurement, show the absolute value and “—” for its change. Keep environment differences and local-control comparisons in the approach records. Show concurrency levels side by side and spell them out, such as “Concurrency 1” and “Concurrency 16”. Run IDs, full configurations and logs belong in linked records.

Keep chart titles short: model, dataset and metric are enough. Mark retained milestones and failed attempts; ordinary measured points need no “valid” label. The main chart shows all summarized approaches. Additional milestone-only or per-condition figures belong with the details. Generate report iteration charts with `foretoken plot --progress`, using its native search and milestone chart types. See the [plotting guide](progress-charts.md).

### Task type

Adapt the following layout to the task's metrics. Values include their change from the original baseline; “—” means unmeasured or undefined, with a reason in the decision column. State the statistic and repeat count. Quality tables retain evaluator sample counts and error estimates. Diagnostics and tool validation stay in the experiment records.

![Approach iteration history](figures/progress-main-search.png)

| Approach | Concurrency 1: tokens/s ↑ | Concurrency 1: ms/token ↓ | Concurrency 16: tokens/s ↑ | Concurrency 16: ms/token ↓ | Decision |
| --- | ---: | ---: | ---: | ---: | --- |
| baseline | Value (—) | Value (—) | Value (—) | Value (—) | Reference |
| [+ Finer prefix matching](#finer-prefix-matching) | Value (%) | Value (%) | Value (%) | Value (%) | Retained / rejected / under investigation |

Link the chart data and [original results](../experiment-records.md#analyze-results) instead of appending duplicate full measurement tables or artifact inventories. Record client, background-build or output differences that affect interpretation in the approach records. Explain mechanisms and trade-offs in the approach details without repeating the table's values.

## Approach details

Explain each approach in the report using the same names as the summary. Related parameter variants can share a section when their changes and outcomes are identified separately. The following section shows the structure to repeat.

### Finer prefix matching

Motivation: describe the bottleneck or observation that prompted the approach, the expected benefit and the hypothesis being tested.

Implementation: identify the actual code or configuration changes and the reference approach they build on. Explain which changes are combined and how they affect execution.

Results: compare the measurements with that reference, explain the observed gains or regressions, and state whether the approach was retained, rejected or awaits measurement. Distinguish the measured result from its proposed explanation.

Link the corresponding `iterations/<name>/notes/iteration.md`, code or configuration, and original results. Keep complete commands, logs, repeated measurements and review history in those records; the report contains the explanation readers need to understand the approach. Resolve links from the report's location.

## Execution and delivery

Summarize time and resource use as required by the task, including the observation interval and sampling coverage. Link the retained configuration, code and key figures, and identify remaining validation or research questions. State when no approach has been selected. When the experiment pauses or ends, record the reason and actual resource state.
