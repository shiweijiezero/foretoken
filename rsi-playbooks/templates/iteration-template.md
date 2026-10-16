# Iteration Notes

English | [简体中文](iteration-template_zh.md)

Reference for `results/<goal>/<motivation>/iterations/<name>/notes/iteration.md`. Identify the developer, executor, writer, and independent reviewer, and link supporting runs under `../runs/`. Follow the [recording guide](../experiment-records.md#complete-the-experiment-notes) to preserve the history of analysis, reviews, and decisions.

## Question and analysis

State the question and your analysis of its causes. Cite the code, observations, prior results, or references used in that analysis.

## Hypothesis and design

Record the Worker's proposed improvement, expected mechanism, and concrete design. Record the lead's selected comparisons alongside the hypothesis.

## Implementation and deployment

Describe the Worker's delivered changes, linking the checkout, revision or changed files, and explanation. Record the version, environment, and configuration the executor actually built and deployed, including material departures from the design. Identify the checkout held unchanged during execution.

## Measurements and interpretation

Append a separate explanation under each run name: link results under `../runs/<run>/`, explain workload and sample-size choices, compare with the reference result, and state whether the hypothesis was supported. Explain failures or interruptions and their effect on the conclusion, including other possible causes of the observed differences.

## Time spent

Link measured durations and label estimates for investigation, design, implementation, deployment, evaluation, and analysis. Keep overlapping durations separate.

## Behavior and attribution review

Append dated findings attributed to the independent reviewer on benchmark-specific shortcuts, model behavior, comparison conditions, and the source of gains. Link original evidence and state which conclusions are supported or need further measurement. Later corrections reference the earlier finding instead of overwriting it.

## Decision and completion

Append dated decisions with their source and supporting evidence, distinguishing the Worker's candidate outcome from the lead's retention or integration decision. State the action, scope, resulting code/deployment state, and resources needed by the next iteration. If a decision changes, append the replacement and identify the earlier entry it supersedes; preserve the original judgment and its basis.
