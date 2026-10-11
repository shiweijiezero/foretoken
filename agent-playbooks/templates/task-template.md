<!--
SPDX-License-Identifier: Apache-2.0
SPDX-FileCopyrightText: Copyright contributors to the Foretoken project
-->

# Optimization Task Template

English | [简体中文](task-template_zh.md)

Fill in only the conditions that matter to the task. Other fields may say "unrestricted" or "agent-selected", or be omitted. Per-service settings can be fixed constraints or starting points; available resources describe the pool actually available to the whole task.

```text
Execution entry: <working directory or repository, deployment configuration or service URL; omit if already clear from context>
Previous experiments: <existing experiment directories or reports, main findings and open questions; omit if none>
Models: <specified models or a model family, or agent-selected>
Precision: <for example, fixed BF16, a comparison of BF16 and FP8, or unrestricted>
Deployment layout: <for example, colocated serving, prefill/decode disaggregation, or unrestricted>
Per-service configuration: <GPU count and parallelism, CPU cores and memory; fixed, a starting point, or agent-selected>
Available resources: <node count and available GPUs, CPU cores and memory per node; for example, 2*8 C500 GPUs with 64 CPU cores and 512 GiB memory per node>
Maximum parallel experiments: <simultaneous independent experiments, for example 4; or have the agent determine and record the limit from the resource budget>
Evaluation workloads: <specified dataset, length and concurrency groups, or agent-selected>
Optimization objectives: <primary metric and target, conditions to preserve, and per-group or aggregate judgment; open-ended exploration is also valid>
Allowed changes: <for example, deployment parameters, inference engine, routing, or the whole system>
Work and completion conditions: <clock start and total work period or target-based completion; per-experiment budgets may be specified or selected by the agent for each hypothesis>
Deliverables: <full iteration history: motivation, approach and changes, measurements, analysis, time spent and next decision for each iteration; overall gains, retained approach, code changes and result links>
```

The parallel-experiment limit counts independent experiments running at once; request concurrency belongs in the workload. Schedule according to each experiment's resource needs rather than aiming to occupy every resource.

Workloads can reference existing configurations or [recipes](../../benchmarks/docs/recipes.md). For multiple workload groups, specify whether objectives apply to each group or to an aggregate result.

See [optimization tasks](../tasks/README.md#examples) for filled-in examples. Follow the [shared workflow](../README.md#steps) for execution and the [experiment records guide](../experiment-records.md) for results.
