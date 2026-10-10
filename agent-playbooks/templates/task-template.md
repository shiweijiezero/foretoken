<!--
SPDX-License-Identifier: Apache-2.0
SPDX-FileCopyrightText: Copyright contributors to the Foretoken project
-->

# Optimization Task Template

English | [简体中文](task-template_zh.md)

Copy and fill in this task description. Mark per-service settings as fixed constraints or starting points for exploration. Available resources belong to the whole task and can be allocated across parallel experiments.

```text
Models: <model names; one or more>
Precision: <for example, fixed BF16 or a comparison of BF16 and FP8>
Deployment layout: <for example, colocated serving or prefill/decode disaggregation>
Per-service configuration: <GPU count and TP/PP/DP/EP settings; identify fixed settings or allowed changes>
Available resources: <for example, 2*8 C500 GPUs>
Evaluation workloads: <datasets or real requests, input/output lengths, concurrency or request rate; one or more groups>
Optimization objectives: <throughput, latency, quality or resource-efficiency targets for each group or overall>
Allowed changes: <for example, deployment parameters, inference engine, routing, or the whole system>
Work and completion conditions: <for example, optimize for 12 hours, finish on reaching the target, or reach it within 12 hours>
Deliverables: <retained approach, code changes, result comparisons and experiment records>
```

TP, PP, DP and EP mean tensor, pipeline, data and expert parallelism. Workloads can reference existing configurations or [recipes](../../benchmarks/docs/recipes.md). For multiple workload groups, specify whether objectives apply to each group or to an aggregate result.

See [optimization tasks](../tasks/README.md#examples) for filled-in examples. Follow the [shared workflow](../README.md#steps) for execution and the [experiment records guide](../experiment-records.md) for results.
