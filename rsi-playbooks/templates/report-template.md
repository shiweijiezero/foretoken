# Optimization results

English | [简体中文](report-template_zh.md)

Use one continuing `report.md`. The designated overview owner keeps the metadata, status and short interpretation current under the task-local `.foretoken-report.lock`. Experimenters update their own iteration records and refresh the generated progress view; do not copy numerical rows into this file.

## Task

| Field | Value |
| --- | --- |
| Objective | Model, optimization target and decision scope |
| Workloads | Request protocol, input/output budgets and concurrency levels |
| Status / updated | Running, paused or complete; ISO timestamp and observation source |
| Overview owner | Native member identity responsible for report prose; successor and handoff time when it changes |
| Budget | Original start, hard deadline and resource ceiling |
| Task / results | Links to the task file and result directory |
| Remaining validation / cleanup | Open quality or performance checks and actual resource exit evidence |

## Generated progress

Link `figures/progress/progress-summary.md` and its matching charts here. That generated summary is the numerical source of truth: it preserves the user's initial baseline, measured parents, scope-specific comparisons, retained/rejected/pending approaches and failed attempts. Keep concurrency levels and workload protocols separate when the generated view does so.

## Approaches

Use the same labels as the generated summary. The overview owner keeps each section short; the full measurements, commands, logs, output checks, independent reviews and decision history stay in the linked iteration record.

### A

Explain the bottleneck, the actual parent, the implemented delta and the decision scope. Link the iteration record and source/configuration evidence.

### A and B

Describe what B adds to the retained A configuration and what the measured combination established. Independent A and B experiments do not establish A+B; a combined configuration needs its own measured record.
