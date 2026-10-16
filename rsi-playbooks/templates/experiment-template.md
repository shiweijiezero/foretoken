# Experiment Notes

English | [简体中文](experiment-template_zh.md)

Reference for `results/<goal>/<motivation>/notes/experiment.md`. Keep one overall summary and link to the individual iteration notes.

## Goal

State the behavior to improve and the success criteria, such as a latency target under a specified workload and the answer quality to preserve.

## Scope

Name the components that may change, or explicitly leave the scope open to the whole system. List the functionality and resource constraints to preserve.

## Comparison

List the models, workloads, and metrics for comparison. State which conditions must remain the same across approaches and link the configurations and reference measurements.

## Current coordination

For a team experiment, keep a short current view of assignments. Link native task records rather than duplicating their status. Without task tools, use one row per active research question:

| Question and next action | Owner and execution scope | Allocation and job/result links | State, dependency or lead decision needed |
| --- | --- | --- | --- |

State the original deadline and any reserved quiet measurement window. Update on handoff, release or a consequential finding; distinguish executing, waiting, blocked and finished work. Keep interpretation and review history in iteration notes.

## Findings

At the end of each iteration, the assigned writer updates this summary with new or revised findings, linking `../iterations/<name>/notes/iteration.md` and supporting results.

## Next questions

List unresolved questions and the hypothesis to test next. When the experiment ends, state whether its goal was met.
