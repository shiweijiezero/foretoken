# Optimization Report

English | [简体中文](report-template_zh.md)

Use this for the task's continuously maintained `report.md`, following the [report workflow](../experiment-records.md#maintain-the-user-facing-report). Deliver a file by default; keep the report updated on a document platform when the user specifies one. Begin with the experiment's objective, current status and last update time.

## Results summary

Briefly state the currently retained approach and measured performance changes. Specify the model, software version, hardware, workload, and concurrency. Use separate tables for different conditions, each starting with its initial reference approach (base).

Select task metrics from the [performance comparison](../experiment-records.md#analyze-results) and link the results. Label each metric with its unit and improvement direction.

| Approach | Change | Metric A (unit) | Change from base | Metric B (unit) | Change from base | Retained? |
| --- | --- | ---: | ---: | ---: | ---: | --- |
| base | Initial approach | Mean ± stddev (n) | — | Mean ± stddev (n) | — | Initial reference |
| Approach name | Specific operation | Mean ± stddev (n) | Percentage change | Mean ± stddev (n) | Percentage change | Actual decision |

Here `n` is the number of measured repeats for that metric; show a single value when only one repeat is available. List iterations in order, including regressions. Preserve missing or undefined values as “—” with their reason. Explain condition differences that affect the conclusion. Report quality scores with their evaluator's sample counts and error estimates.

## Iterations

Give each iteration a short paragraph: why it was tried, what changed, what happened, and whether it was retained. Use the names from the summary table and link original results. Explain operations and effects in natural language, briefly defining necessary terms on first use.

## Currently retained approach

Describe the retained changes, measured effect and pending validation. State when no approach has been selected yet. Link configurations, code and figures. Briefly summarize time and resource use as required by the task, with the observation interval and sampling coverage; keep full commands and logs in run records. When the experiment pauses or ends, explain why and what remains unresolved.
