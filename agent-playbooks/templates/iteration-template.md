# Iteration Notes

English | [简体中文](iteration-template_zh.md)

Reference for `results/<goal>/<motivation>/iterations/<name>/notes/iteration.md`. Update this note throughout the iteration and link run records under `../runs/`.

## Question and analysis

State the question and your analysis of its causes. Cite the code, observations, prior results, or references used in that analysis.

## Hypothesis and design

Describe the proposed improvement, why it should work, and the concrete design. Identify measurements that would support or contradict the hypothesis.

## Implementation and deployment

After implementation, describe the actual changes and link source and configuration records. Identify the deployed version and material departures from the design.

## Measurements and interpretation

Explain the workload and sample-size choices. Link each run and explain how failures or interruptions affect the conclusions. Compare against the reference results, state whether the hypothesis was supported, and consider other conditions that could explain the differences.

## Time spent

Link measured durations and label estimates for investigation, design, implementation, deployment, evaluation, and analysis. Keep overlapping durations separate.

## Decision

State the decision to retain, refine, or revert changes and the resulting code and deployment state. Name the next question, link the updated experiment summary and guidance, and record which temporary resources were cleaned up or retained and why.

## Optional behavior and attribution review

If a review was performed, record findings on benchmark-specific shortcuts, model behavior, and the source of gains. Link evidence and unresolved questions.
