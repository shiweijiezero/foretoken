<!--
SPDX-License-Identifier: Apache-2.0
SPDX-FileCopyrightText: Copyright contributors to the Foretoken project
-->

# 03 | Exploration and Decisions

English | [简体中文](03-exploration-and-decision_zh.md)

Explorers use Pareto-Guided Exploration (PGE) to choose research that can improve the fastest validated deployment available to the user. Workers turn those findings into integrated changes, and Experimenters measure them. The outcome is a better usable configuration, not a larger collection of isolated experiments.

## Continue from the best usable configuration

Start from the latest retained configuration for the task and workload. Read its implementation, measured parents and remaining bottlenecks before branching. The original baseline remains the reference for cumulative improvement; it is not the default development base.

When another branch contributes a useful change, bring it into the applicable retained configuration and measure the combination. Record the actual parent, what this candidate adds, the incremental result and the adoption decision. Refresh a candidate's parent during preparation when relevant changes arrive, while preserving any experiment already running. Use older versions for a specific reproduction or attribution question.

Keep the fastest validated configuration available while research continues. A local kernel gain or a promising component is an intermediate finding; integration must establish its benefit under the user's workload. Different workload trade-offs may require more than one retained configuration.

## Compare opportunities with PGE

Begin with the main costs and constraints, using papers, official implementations, local source and existing measurements. Consider changes to algorithms, operators, data movement and scheduling, including alternatives to the starting architecture. Important mechanisms deserve sustained implementation work even when smaller changes are easier to measure.

Compare potential gain, feasibility, supporting evidence and additional effort. Keep measured results distinct from projections, and use ranges or unknowns where needed. A direction is dominated when another is no worse on the compared objectives and better on at least one. Pareto filtering removes clearly weaker choices; the Explorer still chooses among the remaining trade-offs using their importance and expected learning.

For example, suppose two approaches address the same bottleneck and validation milestone. One offers an estimated 4–6% gain for 3–4 hours of additional work; another offers 8–10% for 1–2 hours. The second dominates under those assumptions. If either estimate depends on an untested mechanism, name that uncertainty and choose work that can resolve it rather than treating the estimate as a measured gain.

Use the actual critical path to estimate headroom. If fraction `f` of elapsed time is accelerated by `s` with added overhead fraction `a`, a useful bound is `1 / (1 - f + f/s + a)`. A component occupying 4% of elapsed time offers at most about 4.17% throughput improvement if removed. Larger potential may require changing the surrounding execution path.

Record the comparison in existing research notes when it changes a decision. Raw gains, assumptions and effort estimates are usually sufficient; recurring score sheets are not required. Evaluators inspect whether exploration and resource use are producing useful progress, rather than taking over direction selection.

## Turn a question into cumulative progress

The research owner chooses the next hypothesis, implementation and informative comparison within the task's total budget. Share useful findings directly with the relevant Worker or Experimenter. A stage delivery or completed review is a point of progress, not the end of an ongoing exploration responsibility.

Use new results to choose the next substantial change:

- When a gain appears, integrate it and examine the remaining bottleneck.
- When a local speedup disappears in the service, account for added conversions, synchronization, batching and scheduling costs.
- When repeated parameter changes yield similar outcomes, reconsider their shared mechanism.
- When a prototype fails or slows down, distinguish a correctable implementation cost from evidence against the hypothesis.

Choose measurements by the decision they can inform. Reuse applicable output checks, controls and profiling evidence; repeat a comparison only to resolve a concrete uncertainty. Research can proceed while environments are prepared, and independent candidates use separate worktrees and deployments. Repair recurring execution defects at their owner and verify the affected path before relying on the repair.

Stop or redirect a line when its question is answered, evidence contradicts its premise or further work is no longer worthwhile in the remaining budget. Keep the finding and return useful changes to the retained configuration. Soft stage estimates can change without routine root approval; the task's fixed requirements and hard deadline remain in force.
