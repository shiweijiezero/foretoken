<!--
SPDX-License-Identifier: Apache-2.0
SPDX-FileCopyrightText: Copyright contributors to the Foretoken project
-->

# Optimization Task Template

English | [简体中文](task-template_zh.md)

Fill in only the conditions that matter to the task. Other fields may say "unrestricted" or "agent-selected", or be omitted. Per-service settings can be fixed constraints or starting points; available resources describe the pool actually available to the whole task.

```text
Models: <specified models or a model family, or agent-selected>
Precision: <for example, fixed BF16, a comparison of BF16 and FP8, or unrestricted>
Deployment layout: <for example, colocated serving, prefill/decode disaggregation, or unrestricted>
Per-service configuration: <GPU count and parallelism; fixed, a starting point, or agent-selected>
Available resources: <for example, 2*8 C500 GPUs>
Evaluation workloads: <specified dataset, length and concurrency groups, or agent-selected>
Optimization objectives: <throughput, latency, quality or resource-efficiency targets for each group or overall>
Allowed changes: <for example, deployment parameters, inference engine, routing, or the whole system>
Work and completion conditions: <for example, optimize for 12 hours, finish on reaching the target, or reach it within 12 hours>
Deliverables: <retained approach, code changes, result comparisons and experiment records>
```

Workloads can reference existing configurations or [recipes](../../benchmarks/docs/recipes.md). For multiple workload groups, specify whether objectives apply to each group or to an aggregate result.

See [optimization tasks](../tasks/README.md#examples) for filled-in examples. Follow the [shared workflow](../README.md#steps) for execution and the [experiment records guide](../experiment-records.md) for results.
