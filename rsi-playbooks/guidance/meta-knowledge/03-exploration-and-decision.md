<!--
SPDX-License-Identifier: Apache-2.0
SPDX-FileCopyrightText: Copyright contributors to the Foretoken project
-->

# 03 | Exploration and Decision-Making

English | [简体中文](03-exploration-and-decision_zh.md)

Understand the current system and relevant knowledge, then actively choose worthwhile questions, organize research and implementation, and keep working toward real improvements under the target workload.

## Choose questions worth pursuing

Start from the task objective, model principles and hardware constraints. Read relevant papers, official documentation and community implementations alongside local source, and compare their mechanisms, assumptions and results. Use runtime observations and earlier experiments to refine that understanding; method research can begin while the environment and baseline are being prepared. Identify questions still worth investigating:

- Which costs affect the main workloads, and could addressing them yield a substantial improvement?
- What have existing approaches solved, and what limitations remain?
- Which alternative ideas merit comparison, and what knowledge or experiments are missing?
- What resources do investigation, implementation, and validation require, and how can they be organized to sustain progress?

Choose directions by their importance and potential benefit. Do not limit work to superficial tuning and easy changes because deeper work is large or difficult. Invest the research and implementation effort needed for worthwhile algorithmic, operator, or architectural innovation; use effort estimates to plan stages and collaboration.

Some ideas need code before anyone can tell whether they are faster. A trial is worthwhile when the idea addresses the task, has a plausible explanation and can be tested in an informative comparison. Once measurements are available, use correctness, quality and service performance to decide whether to keep it.

## Turn findings into research tasks

Use references, source code, and measurements to formulate concrete questions. Investigate unexplained mechanisms, seek alternative methods, and look for counterexamples to current assumptions. New questions can emerge independently of the candidates already being implemented.

Explain how the sources and code you studied led to the current idea: what existing methods solve, how their assumptions differ from this task, and what remains to be learned. If no ready-made switch, script or tuning tool exists, build the part needed for the comparison using existing components. The owner continues analyzing results and revising the approach within the agreed scope and budget.

## Keep digging as results emerge

Once a direction is chosen, give source analysis, experiments, and implementation sustained attention. After each improvement takes effect, measure and analyze the system again, identify new bottlenecks and design choices still open to change, and formulate the next question. Revisit areas already investigated as new results emerge, continuing within the task objectives and budget. Use intermediate results to revise understanding and refine the approach:

- A gain appears: explain why it works and under what conditions, then address the remaining costs.
- Results are inconclusive: confirm the implementation took effect, inspect the expected savings and added costs, then revise the design.
- Local speedups yield limited service gains: trace the integrated execution and investigate batching, scheduling, data placement, and effects on other stages.
- Parameter settings repeatedly produce similar results: reconsider their shared execution strategy and explore different algorithms, parallel dimensions, or ways to organize work.

Larger changes can be divided into key hypotheses, a core implementation, and full integration. Address the technical questions that most affect feasibility first, then complete execution with real requests.

A promising implementation may be slower at first. For example, a layout change can add conversions that a later fusion removes, reducing the main memory cost. Continue investigating when code analysis, profiles or measurements support that explanation. Explain the current slowdown and what the next experiment should establish, then set a budget for pursuing the new path.

Keep the best validated approach available while exploring, and record slowdowns as well as gains. Reconsider a direction when its question is answered, evidence contradicts the explanation, or the budget is exhausted. If you stop, explain whether testing showed no benefit, the current implementation could not run, or the idea has not been implemented or tested. These are different outcomes, and none shows that all other directions lack value. The task's correctness and quality requirements apply throughout exploration.

## Maintain the research direction

Choose experiments by the decision they can inform. For example, when a candidate shows no gain, distinguish an inactive code path from an optimization whose savings are offset elsewhere; select diagnostics or comparisons that separate those explanations. Reuse retained approaches and comparable results. Move on when existing evidence answers the question; repeat or expand measurements to resolve a specific uncertainty, such as variation, tail latency, or stability.

Use environment checks to investigate specific preparation failures. For performance questions, follow the [bottleneck analysis guidance](02-bottleneck-analysis.md): start with existing profiling tools and add temporary instrumentation only when important information is missing. Once the problem is understood, continue implementing or comparing approaches. While the Experimenter prepares or repairs the environment, Advisors continue research and Workers prioritize implementation, exploration and local checks that do not need it.

Reuse Foretoken's existing entry points, caches, and artifacts for execution, including [incremental source updates](../../../docs/deployment/source.md); temporary tools serve questions the existing facilities cannot answer. When the same obstacle recurs across candidates, distinguish incorrect usage or environment configuration from a framework defect. Use the observed delay, likely recurrence, and repair cost to prioritize a focused fix within the task scope. Validate the affected workflow before reusing the fix.

Keep research and delivery moving together. Update records, validate retained changes, and release unused resources as results arrive. Near the deadline, use observed build, run, analysis, and delivery times to choose work that can finish within the budget. A long experiment may give way to a shorter comparison, source investigation, or analysis of saved results. Continue independent work while final validation runs, and stop according to the task's end conditions.

After a group of experiments, repeated similar results, or before stopping a direction, ask which questions are understood and which need a different implementation to answer. An Advisor can point out overlooked evidence or an approach that has not been tried, and suggest the most useful next step in the remaining time. Finishing a parameter search or validating the current best approach does not settle these questions.

## Explore and exchange findings in parallel

Pursue complete research questions in parallel, sharing objectives and existing findings while leaving room for independent thinking. Discuss mechanisms, alternatives, and counterexamples as they emerge. Connect local findings to the end-to-end execution path so the team can explain their effects on the service.

Share reusable implementations, rejected hypotheses, new constraints, and changes that may work together. As results arrive, continue exploring mechanisms and methods beyond the current candidate list. Follow the [team guidance](../../../.agents/skills/rsi/roles/common.md) for assignments, execution, and resource handoffs.
