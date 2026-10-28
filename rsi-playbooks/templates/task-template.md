<!--
SPDX-License-Identifier: Apache-2.0
SPDX-FileCopyrightText: Copyright contributors to the Foretoken project
-->

# Optimization Task Template

English | [简体中文](task-template_zh.md)

Fill in only the conditions that matter to the task. Other fields may say "unrestricted" or "agent-selected", or be omitted. State the final deployment budget separately from an optional starting configuration; available resources describe the pool actually available to the whole task.

```text
Execution entry: <working directory or repository, deployment configuration or service URL; omit if already clear from context>
Starting configuration: <optional maintained deployment directory or known working configuration; omit to let the agent discover an initial approach>
Previous experiments: <existing experiment directories or reports, main findings and open questions; omit if none>
Models: <specified models or a model family, or agent-selected>
Precision: <for example, fixed BF16, a comparison of BF16 and FP8, or unrestricted>
Deployment layout: <for example, colocated serving, prefill/decode disaggregation, or unrestricted>
Final deployment budget: <node placement and total GPU, CPU, and memory limits for the retained service; specify which workloads are included, or leave agent-selected>
Available resources: <node count and available GPUs, CPU cores and memory per node; for example, 2*8 C500 GPUs with 64 CPU cores and 512 GiB memory per node>
Explorer count: <concurrent subagents in this role, excluding the lead; a nonnegative integer, or leave blank for the lead to choose>
Worker count: <concurrent subagents in this role, excluding the lead; a nonnegative integer, or leave blank for the lead to choose>
Experimenter count: <concurrent subagents in this role, excluding the lead; a nonnegative integer, or leave blank for the lead to choose>
Observer count: 1
Observer check interval: <time between team checks, for example 1h>
Maximum parallel experiments: <simultaneous independent experiments, for example 4; or have the agent determine and record the limit from the resource budget>
Evaluation workloads: <specified dataset, length and concurrency groups, or agent-selected>
Optimization objectives: <primary metric and target, conditions to preserve, and per-group or aggregate judgment; open-ended exploration is also valid>
Allowed changes: <for example, deployment parameters, inference engine, routing, or the whole system>
Work and completion conditions: <clock start and total work period or target-based completion; per-experiment budgets may be specified or selected by the agent for each hypothesis>
Deliverables: <full iteration history: motivation, approach and changes, measurements, analysis, time spent and next decision for each iteration; overall gains, retained approach, code changes and result links>
```

A starting configuration is an exploration aid, not an additional constraint. Check it against the task and current environment, then evolve it within the allowed changes. Without one, discover an initial deployment and record it before making comparisons. Use prior retained results when their conditions still apply.

Observer count is the number of new agents created for each check, not a permanent team size. It defaults to 1 when omitted or blank; set it to 0 to disable checks. When enabled, specify the interval here. The lead fills in a missing interval before execution and arranges the reminders and fresh agents through the [RSI skill](../../.agents/skills/rsi/SKILL.md#arrange-periodic-team-checks).

The parallel-experiment limit counts independent experiments running at once; request concurrency belongs in the workload. Distinguish the final deployment budget from each experiment's needs. Smaller diagnostic experiments may use fewer devices; allocate the remaining resources to other useful work while preserving comparison conditions.

Workloads can reference existing configurations or [recipes](../../benchmarks/docs/recipes.md). For multiple workload groups, specify whether objectives apply to each group or to an aggregate result.

For filled-in examples, see the Qwen3.5-35B-A3B tasks for [NVIDIA](../tasks/qwen3.5-35b-a3b-nvidia.md) and [MetaX](../tasks/qwen3.5-35b-a3b-muxi.md). Agents follow the [RSI skill](../../.agents/skills/rsi/SKILL.md) for execution and the [experiment records guide](../experiment-records.md) for results.
