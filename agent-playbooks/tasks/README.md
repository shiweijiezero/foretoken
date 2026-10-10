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

The available resource pool is 2*8 C500 GPUs. Start with Qwen3.5-35B-A3B in BF16, with two-GPU tensor parallelism (TP2) per service, and optimize autonomously for 12 hours, exploring approaches in parallel. Evaluate input/output lengths, concurrency levels, and natural conversation workloads to improve throughput and reduce latency, then summarize results for each workload.

The available resource pool is 2*8 C500 GPUs. Fix each candidate service to two GPUs on one host, BF16, and TP2 for Qwen3.5-35B-A3B, and compare optimization approaches in parallel. With 8,192 input tokens, 512 output tokens, and concurrency 1, reduce mean time per output token (TPOT) to 5 ms or less.

The available resource pool is 2*8 C500 GPUs. For Qwen3.5-35B-A3B in BF16, select per-service resources and parallelism separately for colocated serving and prefill/decode disaggregation, and run experiments in parallel. Allow 12 hours to reduce p95 time to first token to 1 second or less in each agreed workload group spanning short and long inputs and different concurrency levels; summarize the results when the target is reached or the time budget expires.
