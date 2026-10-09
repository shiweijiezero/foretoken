# Optimization Tasks

English | [简体中文](README_zh.md)

These tasks define goals, scope, constraints, and deliverables. The user selects a task and supplies the environment and necessary constraints; the agent follows the [shared workflow](../README.md#steps) for analysis, design, implementation, evaluation, and recording.

## Autonomous execution

Continue working within the task scope and resource budget. Judge completion by the objective and measured results.

- Carry investigation, design, implementation, deployment, evaluation, and analysis through to completion. Resolve problems that can be handled independently and proceed with established next steps.
- Use evidence to choose the next action after each iteration. Investigate ineffective approaches, refine or replace them, and assess the remaining gap to the objective after a partial improvement.
- Treat code changes, service startup, and individual test runs as progress rather than task completion. Support conclusions with performance and quality measurements under actual workloads.
- Keep experiment records current. At completion, state which objectives were met, which changes were retained, and where results are stored. If the budget is exhausted or progress requires outside intervention, identify the concrete blocker and what is needed to resume.

For example: optimize inference autonomously for 12 hours, iterating throughout, then summarize the results and clean up temporary resources.

Reduce TPOT to 20 ms or less for the agreed model, precision, and workload; finish after confirming the result in a repeat run.

Reach that target within 12 hours; wrap up when the target is confirmed or the time budget expires, whichever comes first.
