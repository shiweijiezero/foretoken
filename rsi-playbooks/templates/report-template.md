# Optimization Report

English | [简体中文](report-template_zh.md)

Use this for the task's continuously maintained `report.md`, following the [report workflow](../experiment-records.md#maintain-the-user-facing-report). Deliver a file by default; keep the report updated on a document platform when the user specifies one. Begin with the experiment's objective, current status and last update time. For a time-bounded task, include its original start and deadline. Link the task and experiment result directory.

## Results summary

Briefly state the currently retained approach and measured performance changes. Specify the model, software version, hardware, workload, and concurrency. Use separate tables for different conditions, each starting with its initial reference approach (base).

Select task metrics from the [performance comparison](../experiment-records.md#analyze-results) and link the results. Label each metric with its unit and improvement direction.

| Approach | Change | Metric A (unit) | Change from base | Metric B (unit) | Change from base | Retained? |
| --- | --- | ---: | ---: | ---: | ---: | --- |
| base | Initial approach | Mean ± stddev (n) | — | Mean ± stddev (n) | — | Initial reference |
| Approach name | Specific operation | Mean ± stddev (n) | Percentage change | Mean ± stddev (n) | Percentage change | Actual decision |

Here `n` is the number of measured repeats for that metric; show a single value when only one repeat is available. List iterations in order, including regressions. Preserve missing or undefined values as “—” with their reason. Explain condition differences that affect the conclusion. Report quality scores with their evaluator's sample counts and error estimates.

## Iterations

Introduce each approach or research question with a short summary, including work awaiting measurement. Use the names from the results summary and link its `iterations/<name>/notes/iteration.md`, which leads to the complete plans, analyses and original results. Resolve links from the report's location.

| Iteration | Approach and findings | Detailed notes |
| --- | --- | --- |

## Currently retained approach

Describe the retained changes, measured effect and pending validation. State when no approach has been selected yet. Link configurations, code and figures. Briefly summarize time and resource use as required by the task, with the observation interval and sampling coverage; keep full commands and logs in run records. State the next question or comparison and any unresolved disagreements, with links to supporting notes. When the experiment pauses or ends, explain why.
