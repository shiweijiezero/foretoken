# Optimization Tasks

English | [简体中文](README_zh.md)

These tasks define goals, scope, constraints, and deliverables. Use the [task template](../templates/task-template.md) to specify the model, resources, workloads, and objectives; the agent follows the [shared workflow](../README.md#steps) for analysis, design, implementation, evaluation, and recording.

## Autonomous execution

Continue working within the task scope and resource budget. Judge completion by the objective and measured results.

- Carry investigation, design, implementation, deployment, evaluation, and analysis through to completion. Resolve problems that can be handled independently and proceed with established next steps.
- Use evidence to choose the next action after each iteration. Investigate ineffective approaches, refine or replace them, and assess the remaining gap to the objective after a partial improvement.
- Treat code changes, service startup, and individual test runs as progress rather than task completion. Support conclusions with performance and quality measurements under actual workloads.
- Keep experiment records current. At completion, state which objectives were met, which changes were retained, and where results are stored. If the budget is exhausted or progress requires outside intervention, identify the concrete blocker and what is needed to resume.

## Examples

```text
Models: Qwen3.5-35B-A3B
Precision: unrestricted
Deployment layout: unrestricted
Per-service configuration: agent-selected GPU count, parallelism, CPU cores and memory
Available resources: 2*8 C500 GPUs with 64 available CPU cores and 512 GiB memory per node, allocatable across parallel experiments
Evaluation workloads: agent-selected, covering input/output lengths, concurrency levels and natural conversations
Optimization objectives: improve throughput and reduce latency; compare performance and answer quality for each workload
Allowed changes: unrestricted
Work and completion conditions: optimize autonomously for 12 hours
Deliverables: full iteration history with motivation, approach and changes, measurements, analysis, time spent and next decision for each iteration; per-workload gains, retained approach and code changes
```

```text
Models: Qwen3.5-35B-A3B
Precision: fixed BF16
Deployment layout: colocated serving
Per-service configuration: fixed two GPUs on one host with TP2, 32 CPU cores and 128 GiB memory
Available resources: 2*8 C500 GPUs with 64 available CPU cores and 512 GiB memory per node, allocatable across parallel experiments
Evaluation workloads: 8,192 input tokens, 512 output tokens, concurrency 1
Optimization objectives: reduce mean time per output token (TPOT) to 5 ms or less
Allowed changes: unrestricted
Work and completion conditions: finish when the target is reached
Deliverables: full iteration history with motivation, approach and changes, measurements, analysis, time spent and next decision for each iteration; per-workload gains, retained approach and code changes
```

```text
Models: Qwen3.5-35B-A3B
Precision: fixed BF16
Deployment layout: compare colocated serving with prefill/decode disaggregation
Per-service configuration: agent-selected GPU count, parallelism, CPU cores and memory
Available resources: 2*8 C500 GPUs with 64 available CPU cores and 512 GiB memory per node, allocatable across parallel experiments
Evaluation workloads: combinations of 1,024/8,192 input tokens, 128/512 output tokens and concurrency 1/8
Optimization objectives: reduce p95 time to first token to 1 second or less for every workload group
Allowed changes: unrestricted
Work and completion conditions: reach the target within 12 hours; summarize when the target is reached or the time budget expires
Deliverables: full iteration history with motivation, approach and changes, measurements, analysis, time spent and next decision for each iteration; per-workload gains, retained approach and code changes
```
