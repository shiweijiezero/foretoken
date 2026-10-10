# Optimization Tasks

English | [简体中文](README_zh.md)

These tasks define goals, scope, constraints, and deliverables. Use the [task template](../templates/task-template.md) to specify the model, resources, workloads, and objectives; the agent follows the [shared workflow](../README.md#steps) for analysis, design, implementation, evaluation, and recording.

## Autonomous execution

Continue working within the task scope and resource budget. Judge completion by the objective and measured results.

- When earlier experiments exist, read their records, check the conditions, and continue from retained approaches, ruled-out directions and unresolved questions. Reuse comparable results and spend resources on new questions; if changed conditions or insufficient evidence call for a repeat, state what it should establish.
- Make the build, deployment, measurement, recording and comparison workflow usable with existing tools. Address recurring delays or tooling gaps at their owning entrypoints so the improvement shortens subsequent experiment cycles.
- Carry investigation, design, implementation, deployment, evaluation, and analysis through to completion. Resolve problems that can be handled independently and proceed with established next steps.
- Use evidence to choose the next action after each iteration. Investigate ineffective approaches, refine or replace them, and assess the remaining gap to the objective after a partial improvement.
- Use resources for informative experiments: schedule independent approaches within the available GPU, CPU, memory and parallel-experiment budget, and advance analysis or implementation that does not depend on pending measurements. Coordinate shared resources to keep results comparable.
- Keep iterations short, using enough requests or measurement time to judge the current hypothesis. Increase samples, repeat measurements or extend a run when gains are uncertain or tail latency or stability needs closer evaluation, and state the purpose. The total work period includes analysis, implementation and deployment; it is not a continuous load-test duration.
- Treat code changes, service startup, and individual test runs as progress rather than task completion. Support conclusions with measurements under actual workloads; evaluate answer quality when changes affect generation behavior.
- Keep experiment records current. At completion, state which objectives were met, which changes were retained, and where results are stored. If the budget is exhausted or progress requires outside intervention, identify the concrete blocker and what is needed to resume.

Reference task: [Qwen throughput and decode latency](qwen-decode.md).

## Examples

```text
Models: Qwen3.5-35B-A3B
Precision: unrestricted
Deployment layout: unrestricted
Per-service configuration: agent-selected GPU count, parallelism, CPU cores and memory
Available resources: 2*8 C500 GPUs with 64 available CPU cores and 512 GiB memory per node
Maximum parallel experiments: 4
Evaluation workloads: agent-selected, covering input/output lengths, concurrency levels and natural conversations
Optimization objectives: improve throughput and reduce latency
Allowed changes: unrestricted
Work and completion conditions: optimize autonomously for 12 hours from the start of task execution
Deliverables: full iteration history with motivation, approach and changes, measurements, analysis, time spent and next decision for each iteration; per-workload gains, retained approach, code changes and result links
```

```text
Models: Qwen3.5-35B-A3B
Precision: fixed BF16
Deployment layout: colocated serving
Per-service configuration: fixed two GPUs on one host with TP2, 32 CPU cores and 128 GiB memory
Available resources: 2*8 C500 GPUs with 64 available CPU cores and 512 GiB memory per node
Maximum parallel experiments: 4
Evaluation workloads: 8,192 input tokens, 512 output tokens, concurrency 1
Optimization objectives: reduce mean time per output token (TPOT) to 5 ms or less
Allowed changes: unrestricted
Work and completion conditions: finish when the target is reached
Deliverables: full iteration history with motivation, approach and changes, measurements, analysis, time spent and next decision for each iteration; per-workload gains, retained approach, code changes and result links
```

```text
Models: Qwen3.5-35B-A3B
Precision: fixed BF16
Deployment layout: compare colocated serving with prefill/decode disaggregation
Per-service configuration: agent-selected GPU count, parallelism, CPU cores and memory
Available resources: 2*8 C500 GPUs with 64 available CPU cores and 512 GiB memory per node
Maximum parallel experiments: 4
Evaluation workloads: combinations of 1,024/8,192 input tokens, 128/512 output tokens and concurrency 1/8
Optimization objectives: reduce p95 time to first token to 1 second or less for every workload group
Allowed changes: unrestricted
Work and completion conditions: reach the target within 12 hours from the start of task execution; summarize when the target is reached or the time budget expires
Deliverables: full iteration history with motivation, approach and changes, measurements, analysis, time spent and next decision for each iteration; per-workload gains, retained approach, code changes and result links
```
