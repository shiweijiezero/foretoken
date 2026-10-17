<!--
SPDX-License-Identifier: Apache-2.0
SPDX-FileCopyrightText: Copyright contributors to the Foretoken project
-->

# 03 | Exploration and Decision-Making

English | [简体中文](03-exploration-and-decision_zh.md)

Understand the current system and relevant knowledge, then actively choose worthwhile questions, organize research and implementation, and keep working toward real improvements under the target workload.

## Choose questions worth pursuing

Study relevant research and upstream implementations, current runtime behavior, and the results and trade-offs of previous experiments. Understand the model principles, execution mechanisms, and hardware characteristics involved. Use the task objectives to identify questions still worth investigating:

- Which costs affect the main workloads, and could addressing them yield a substantial improvement?
- What have existing approaches solved, and what limitations remain?
- Which alternative ideas merit comparison, and what knowledge or experiments are missing?
- What resources do investigation, implementation, and validation require, and how can they be organized to sustain progress?

Choose directions by their importance and potential benefit. Do not limit work to superficial tuning and easy changes because deeper work is large or difficult. Invest the research and implementation effort needed for worthwhile algorithmic, operator, or architectural innovation; use effort estimates to plan stages and collaboration.

## Turn findings into research tasks

Use references, source code, and measurements to formulate concrete questions. Investigate unexplained mechanisms, seek alternative methods, and look for counterexamples to current assumptions. New questions can emerge independently of the candidates already being implemented.

For each investigation, state what is known, what should change, and how the next step will produce useful results. If no ready-made switch, script, or tuner exists, reuse upstream capabilities or construct a focused experiment for the question, then proceed into implementation.

## Keep digging as results emerge

Once a direction is chosen, give source analysis, experiments, and implementation sustained attention. After each improvement takes effect, measure and analyze the system again, identify new bottlenecks and design choices still open to change, and formulate the next question. Revisit areas already investigated as new results emerge, continuing within the task objectives and budget. Use intermediate results to revise understanding and refine the approach:

- A gain appears: explain why it works and under what conditions, then address the remaining costs.
- Results are inconclusive: confirm the implementation took effect, inspect the expected savings and added costs, then revise the design.
- Local speedups yield limited service gains: trace the integrated execution and investigate batching, scheduling, data placement, and effects on other stages.
- Parameter settings repeatedly produce similar results: reconsider their shared execution strategy and explore different algorithms, parallel dimensions, or ways to organize work.

Larger changes can be divided into key hypotheses, a core implementation, and full integration. Address the technical questions that most affect feasibility first, then complete execution with real requests.

A promising implementation may be slower at first. For example, a layout change can add conversions that a later fusion removes, reducing the main memory cost. Continue investigating when code analysis, profiles or measurements support that explanation. Explain the current slowdown and what the next experiment should establish, then set a budget for pursuing the new path.

Keep the best validated candidate available while exploring the new path, and record regressions alongside gains. Reconsider or stop the direction when evidence contradicts its mechanism or the allocated budget is exhausted. Preserve the task's correctness and quality requirements throughout exploration.

## Maintain the research direction

Choose experiments by the decision they can inform. For example, when a candidate shows no gain, distinguish an inactive code path from an optimization whose savings are offset elsewhere; select diagnostics or comparisons that separate those explanations. Reuse retained approaches and comparable results. Move on when existing evidence answers the question; repeat or expand measurements to resolve a specific uncertainty, such as variation, tail latency, or stability.

Reuse Foretoken's existing entry points, caches, and artifacts for execution, including [incremental source updates](../../../docs/custom-deployment.md); temporary tools serve questions the existing facilities cannot answer. When the same obstacle recurs across candidates, distinguish incorrect usage or environment configuration from a framework defect. Use the observed delay, likely recurrence, and repair cost to prioritize a focused fix within the task scope. Validate the affected workflow before reusing the fix.

Keep research and delivery moving together. Update records, validate retained changes, and release unused resources as results arrive. Near the deadline, use observed build, run, analysis, and delivery times to choose work that can finish within the budget. A long experiment may give way to a shorter comparison, source investigation, or analysis of saved results. Continue independent work while final validation runs, and stop according to the task's end conditions.

After a group of experiments, repeated similar results, or before summarizing, reconsider whether important hotspots have been explained, whether the investigation remains confined to the same execution strategy, and which deeper approaches deserve a trial. Another agent can read the code and evidence to identify omissions, counterexamples, and new design ideas; the lead makes the overall judgment.

## Explore and exchange findings in parallel

Pursue complete research questions in parallel, sharing objectives and existing findings while leaving room for independent thinking. Discuss mechanisms, alternatives, and counterexamples as they emerge. Connect local findings to the end-to-end execution path so the team can explain their effects on the service.

Share reusable implementations, rejected hypotheses, new constraints, and changes that may work together. As results arrive, continue exploring mechanisms and methods beyond the current candidate list. Follow the [team guidance](../../../.agents/skills/rsi/roles/common.md) for assignments, execution, and resource handoffs.
