# Iteration Notes

English | [简体中文](iteration-template_zh.md)

Reference for `results/<goal>/<motivation>/iterations/<name>/notes/iteration.md`. Identify the developer, executor, writer, and independent reviewer, and link supporting runs under `../runs/`. Follow the [recording guide](../experiment-records.md#complete-the-experiment-notes) to preserve the history of analysis, reviews, and decisions.

Link task records for ownership and scheduling. Without task tools, record current owners, dependencies, resource allocations and measurement windows here.

## Summary and documents

Introduce each topic with a short summary and link its complete document in this notes directory, such as `plan.md` or `analysis.md`. Keep concise findings and the review and decision history below; put full explanations in the linked files.

| Topic | Summary | Full document |
| --- | --- | --- |

## Question and analysis

Summarize the question and link the full analysis with its code, observations, prior results, and references.

## Hypothesis and design

Summarize the Worker's proposed improvement and expected mechanism, linking the complete proposal and implementation and validation plan. Record the lead's selected comparisons alongside the hypothesis.

## Implementation and deployment

Summarize the Worker's delivered changes, linking the checkout, revision or changed files, and full explanation. Record the version, environment, and configuration the executor actually built and deployed, including material departures from the design. Identify the checkout held unchanged during execution.

## Measurements and interpretation

Append a concise finding for each run, linking results under `../runs/<run>/` and the full analysis. State whether the hypothesis was supported and how failures or interruptions affect the conclusion. Keep workload and sample-size rationale, comparisons, and detailed explanations of the observed differences in the linked analysis.

## Time spent

Link measured durations and label estimates for investigation, design, implementation, deployment, evaluation, and analysis. Keep overlapping durations separate.

## Behavior and attribution review

Append dated findings attributed to the independent reviewer on benchmark-specific shortcuts, model behavior, comparison conditions, and the source of gains. Link the full review and original evidence, and state which conclusions are supported or need further measurement. Later corrections reference the earlier finding instead of overwriting it.

## Decision and completion

Append dated decisions with their source and supporting evidence, distinguishing the Worker's candidate outcome from the lead's retention or integration decision. State the action, scope, resulting code/deployment state, and resources needed by the next iteration. If a decision changes, append the replacement and identify the earlier entry it supersedes; preserve the original judgment and its basis.
