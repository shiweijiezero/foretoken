# Optimization Tasks

English | [简体中文](README_zh.md)

These tasks define goals, scope, constraints, and deliverables. The user selects a task and supplies the environment and necessary constraints; the agent follows the [shared workflow](../README.md#steps) for analysis, design, implementation, evaluation, and recording.

## Autonomous execution

Continue working within the task scope and resource budget. Judge completion by the objective and measured results.

- Carry investigation, design, implementation, deployment, evaluation, and analysis through to completion. Resolve problems that can be handled independently and proceed with established next steps.
- Use evidence to choose the next action after each iteration. Investigate ineffective approaches, refine or replace them, and assess the remaining gap to the objective after a partial improvement.
- Treat code changes, service startup, and individual test runs as progress rather than task completion. Support conclusions with performance and quality measurements under actual workloads.
- Keep experiment records current. At completion, state which objectives were met, which changes were retained, and where results are stored. If the budget is exhausted or progress requires outside intervention, identify the concrete blocker and what is needed to resume.

For example: two servers with eight C500 GPUs each are available. Start with Qwen3.5-35B-A3B in BF16 on one two-GPU tensor-parallel replica (TP2). Optimize autonomously for 12 hours using 8,192 input tokens, 512 output tokens, and concurrency 1; explore parallelism configurations and speculative decoding, then summarize throughput and latency changes.

Deploy Qwen3.5-35B-A3B in BF16 with TP2 on two C500 GPUs in one server. With 8,192 input tokens, 512 output tokens, and concurrency 1, reduce mean time per output token (TPOT) to 5 ms or less.

Use two servers with eight C500 GPUs each to run Qwen3.5-35B-A3B in BF16, comparing colocated serving with prefill/decode disaggregation. Allow 12 hours to reduce p95 time to first token to 1 second or less with 8,192 input tokens, 512 output tokens, and concurrency 16; summarize the results when the target is reached or the time budget expires.
