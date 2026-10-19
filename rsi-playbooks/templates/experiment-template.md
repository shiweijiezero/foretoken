# Experiment Notes

English | [简体中文](experiment-template_zh.md)

Reference for `results/<goal>/<motivation>/notes/experiment.md`. Keep one overall summary and link to the individual iteration notes.

## Goal

State the behavior to improve and the success criteria, such as a latency target under a specified workload and the answer quality to preserve.

## Scope

Name the components that may change, or explicitly leave the scope open to the whole system. List the functionality and resource constraints to preserve.

## Comparison

List the models, workloads, and metrics for comparison. State which conditions must remain the same across approaches and link the configurations and reference measurements.

## Iteration overview

Provide the shared experiment directory's host and absolute path. Give each approach or research question a short summary and link its `../iterations/<name>/notes/iteration.md`; that note links the complete proposals, plans and analyses. Follow the [document collaboration guide](../experiment-records.md#share-documents-and-code).

| Approach or question | Summary | Iteration notes |
| --- | --- | --- |

Link existing task records for owners, next actions, resources and dependencies. Without task tools, record those arrangements here. State the original deadline and any reserved quiet measurement window.

## Findings

At the end of each iteration, the assigned writer updates this summary with new or revised findings, linking `../iterations/<name>/notes/iteration.md` and supporting results.

## Next questions

List unresolved questions and the hypothesis to test next. When the experiment ends, state whether its goal was met.
