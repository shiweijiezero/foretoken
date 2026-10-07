# Iteration Notes

English | [简体中文](iteration-template_zh.md)

Reference for `results/<goal>/<motivation>/iterations/<name>/notes/iteration.md`. After each perf/eval command, the developer or agent appends the measurements and interpretation to this note, linking supporting records under `../runs/`.

## Question and analysis

State the question and your analysis of its causes. Cite the code, observations, prior results, or references used in that analysis.

## Hypothesis and design

Describe the proposed improvement, why it should work, and the concrete design. Identify measurements that would support or contradict the hypothesis.

## Implementation and deployment

After implementation, describe the actual changes and link source and configuration records. Identify the deployed version and material departures from the design.

## Measurements and interpretation

Append a separate explanation under each run name: link results under `../runs/<run>/`, explain workload and sample-size choices, compare with the reference result, and state whether the hypothesis was supported. Explain failures or interruptions and their effect on the conclusion, including other possible causes of the observed differences.

## Time spent

Link measured durations and label estimates for investigation, design, implementation, deployment, evaluation, and analysis. Keep overlapping durations separate.

## Decision and completion

State the decision to retain, refine, or revert changes, the resulting code and deployment state, and the next question. Note any resources still needed by the next iteration.

## Optional behavior and attribution review

If a review was performed, record findings on benchmark-specific shortcuts, model behavior, and the source of gains. Link evidence and unresolved questions.
