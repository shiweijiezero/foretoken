<!--
SPDX-License-Identifier: Apache-2.0
SPDX-FileCopyrightText: Copyright contributors to the Foretoken project
-->

# 06 | Pareto-Guided Exploration

English | [简体中文](06-opportunity-assessment_zh.md)

Pareto-Guided Exploration (PGE) organizes agentic inference optimization through opportunity assessment, frontier identification, lead decisions and feedback from experiments.

PGE prioritizes important problems with substantial improvement potential. Its purpose is to discover and realize better mechanisms, including changes to algorithms, operators, dataflow and execution architecture. The lead organizes sustained research around those opportunities, with smaller improvements and independent alternatives progressing in parallel. The Evaluator assesses evidence and actual investment; the lead makes future decisions. Both use the existing assessment and team records.

## Organize research before ranking candidates

First identify which limitations matter to the task and what might change them. Explorers investigate mechanisms beyond the current candidate list. The lead selects the main research questions, gives their owners time to develop core implementations, and arranges experiments that can resolve their major uncertainties. Quick improvements can contribute, but their readiness or low cost does not give them priority over more consequential work.

The opportunity front compares the candidates that can currently be compared; it does not define the boundary of worthwhile research. New mechanisms may lack a gain estimate or a complete integration plan. Record their importance, the mechanism being considered and what is unknown, without inventing favorable numbers or treating `U` as low value. The lead can fund research and prototyping before these estimates exist. Its choice concerns the value of the research program, not just gain per hour from the next experiment.

For example, these hypothetical research questions are at different stages and do not belong in one gain/cost ranking:

| Research question | Available evidence | Important uncertainty | Information the work could produce |
| --- | --- | --- | --- |
| Can a different data layout remove repeated materialization? | Intermediate copies occupy a substantial part of the observed path | Whether consumers can use the new layout without conversion overhead | A working core path and an account of traffic saved versus added |
| Can execution be reorganized to overlap exposed stages? | Profiles show waiting and partly serialized work | Which dependencies are essential and which follow from the implementation | A causal dependency model and a runnable alternative schedule |
| Can a different speculation mechanism reduce cost per accepted token? | Drafting and verification costs are recorded across workloads | Whether the proposed change reduces total work rather than acceptance alone | An explanation or prototype that resolves the combined cost trade-off |

These examples illustrate possible research outputs, not Evaluator assignments or promised speedups. A useful stage may establish a mechanism, expose a limitation or deliver a core implementation before it produces a net service gain. A slow or failing prototype calls for diagnosis of the idea and its realization, rather than immediate rejection of the whole direction.

Group measurements by research question and version; retain workload, reference, stage, sources and the owner's remaining-work estimates. Unknown values remain `U`, inapplicable ones `N/A`. Update changed entries from existing evidence rather than requiring extra runs to fill scores. Quality validation follows output impact and the task's agreed evaluation policy; final retention uses the task's correctness, precision and resource requirements.

## Keep measurements and opportunities distinct

The measured deployment front compares observed performance and resource use under the task's correctness and quality requirements. Applicable prior evidence can establish those requirements; a fresh quality score is not required after each performance run. Record unresolved output effects alongside the speed measurement rather than treating them as confirmed deployment gains.

The research opportunity front compares projected gains and additional costs of recorded directions. Preserve the task's original baseline, use a common reference and metric definition for each comparison, and group incompatible hardware or protocols separately. Prototype-relative remaining gain is separate from total gain against that reference. For multiple required workloads, keep the gain vector so an improvement cannot hide a regression; aggregate only with task-specified weights.

Every estimate names its author, time, assumptions, source and range. Distinguish measured variation, statistical intervals and engineering bounds. Overlapping uncertain measurements do not establish an improvement from their means alone.

## Describe gain, evidence and cost

Keep raw values first. The following 0–4 scores summarize them for discussion; they are ordinal descriptions, not probabilities or adoption thresholds. Use score ranges when values span bands. Keep the same anchors within a task and explain any lead-approved changes before comparing rescored rows.

### Gain and headroom: G

For throughput, gain is `100 × (candidate / reference − 1)` percent; for latency reduction, `100 × (1 − candidate / reference)` percent. Preserve negative values, output lengths and variation. Label measured and projected gains separately; a ceiling alone supplies neither an expected gain nor a lower bound.

| G | Gain per required metric |
| --- | --- |
| 0 | Zero or negative; retain the signed value |
| 1 | Greater than 0%, less than 5% |
| 2 | At least 5%, less than 10% |
| 3 | At least 10%, less than 20% |
| 4 | At least 20% |

Estimate headroom from the relevant critical path. For elapsed-time fraction `f`, local speedup `s` and added overhead fraction `a`, simplified speedup is `S = 1 / (1 − f + f/s + a)`. Under fixed work, throughput gain is `100 × (S − 1)`%; elapsed-time reduction is `100 × (1 − 1/S)`%. Eliminating a component occupying 4% of elapsed time yields at most 4.17% throughput gain; accelerating it 2× yields 2.04%. Overlapping GPU kernel durations cannot be summed into `f`, and latency savings need not translate into serving throughput.

### Feasibility F, evidence E and information I

F describes the mechanism under the task's semantics, precision, hardware and resource constraints, separately from environment readiness. E supports a named conclusion: a well-supported negative result can be E4 with G0. I describes information gained in the observed period, separately from future learning anticipated by the owner.

| Score | F: mechanism feasibility | E: evidence for the stated claim | I: information gained |
| --- | --- | --- | --- |
| 0 | Violates a required invariant or demonstrated bound | No supporting evidence yet | Repeats known facts |
| 1 | Mechanism described; essential assumptions unresolved | Analysis, source or another setting | Reproduces a symptom or checks readiness |
| 2 | Analysis supports the path; integration assumptions remain | Local control or component measurement | Distinguishes an explanation or establishes a useful bound |
| 3 | Dependencies and added costs accounted for within task constraints | Matched service comparison; replication or coverage incomplete | Resolves a key causal or correctness uncertainty |
| 4 | Representative execution demonstrates the mechanism and required invariants | Repeated representative comparisons support the claimed scope | Independently checked finding reused in relevant cases |

Link the supporting evidence, scope and remaining assumptions. Lower F or E can identify what deeper work must resolve; they do not make a well-motivated question less important. Distinguish the performance of the current prototype from the opportunity opened by a different implementation. E includes applicable correctness evidence already available. Code volume, messages and PR counts do not establish I.

### Additional cost: C

Keep spent cost for reflection and additional cost for decisions. Record wall hours, attributable GPU-hours and Agent working hours. Separate preparation and waiting where evidence permits; concurrent wall intervals cannot be added, and open agent turns are not measured productive time. Token or monetary costs require reliable attribution.

Compare costs to the same evidence milestone. A component probe and complete service validation deliver different results. Estimate the next research stage when possible, and retain unknown integration costs as U until the implementation makes them clearer. A complete delivery estimate is not a prerequisite for starting an important research question.

For a remaining resource budget `B > 0`, score additional cost `c` by `r = c/B`. With an exhausted budget, positive additional cost is C0 and zero additional cost is C4. All rows in a snapshot use the same observation time and budget. Keep raw costs across checks: a shrinking budget can lower C without making the method less efficient.

| C (higher means less costly) | Fraction of remaining budget |
| --- | --- |
| 4 | `0 ≤ r ≤ 0.05` |
| 3 | `0.05 < r ≤ 0.15` |
| 2 | `0.15 < r ≤ 0.35` |
| 1 | `0.35 < r ≤ 1` |
| 0 | `r > 1` |

Give `C_time`, `C_gpu` and `C_agent` only for defined budgets. With no budget, retain raw cost and mark C as `U`. Wall budget ends at the task deadline; GPU and Agent budgets reflect available allocations. Unknown cost is U, distinct from a known estimate exceeding budget (C0).

## Assess environment work separately

Environment work earns inference G only from measured inference changes. Record its enabling value as `U_env`: 0, no documented consumer; 1, convenience or unconfirmed delay reduction; 2, measured preparation savings; 3, a documented experiment unblocked; 4, a verified repair unlocking multiple experiments or preventing a reproduced recurring failure.

Estimate net cumulative preparation savings as `N × saving_per_use − remaining_repair_and_validation_time`, with N limited to plausible remaining uses and deployment included. Savings count as task wall time only on the serial critical path; overlapping preparation needs a schedule-based estimate. Show GPU and Agent cost separately. When a repair makes an otherwise impossible experiment executable, identify that experiment rather than inventing saved minutes. Attribute an enabled inference gain once.

The Evaluator reports observed and task-to-date time allocation, coverage and delayed work. Growing environment cost without improved readiness is a finding for the lead, not an automatic percentage limit.

## Identify non-dominated opportunities

Pareto dominance means no worse on every compared objective and strictly better on at least one. Compare raw gain vectors and additional cost dimensions under the lead's common workload, milestone and resource scope. F, E and I annotate the comparison; do not sum or multiply them into a priority or success probability.

For bounded estimates, A robustly dominates B only if every gain lower bound of A is at least B's upper bound and every cost upper bound of A is at most B's lower bound, with one strict inequality. This conclusion holds under the stated bounds and assumptions. Otherwise leave dominance unresolved, including unknown values or incompatible conditions. Coarse score ties do not imply equal raw values.

For a purely numerical example, two paths to the same validation milestone use the same reference: A has gain bounds of 4–6% and additional cost of 3–4 hours; B has 8–10% and 1–2 hours. B dominates A under these assumptions. That comparison concerns these implementations and estimates, not every future implementation of A's underlying idea.

Keep demonstrated constraint violations outside the feasible candidate front with their counterevidence; unresolved feasibility stays unknown. A cheap 1% gain can remain non-dominated without being the best research priority. The Evaluator therefore also examines whether important mechanisms have actual research owners, sustained implementation time and access to experiments, or whether the team has concentrated on small ready changes, reporting and environment work. It reports both progress and neglected opportunities; it does not prescribe the next direction.

The lead responds by choosing or revising the research portfolio: the primary problems, independent alternatives, smaller concurrent work, owners, stage budgets and expected learning or implementation outcomes. Preserve continuity on important questions while evidence develops; do not reassign their owners simply because a cheaper experiment finished first. Reconsider a direction when evidence undermines its potential or further work is no longer justified, distinguishing that from an incomplete or temporarily slower prototype. Subsequent evaluations compare the allocation and progress with these decisions.

## References

[Optuna's multi-objective example](https://optuna.readthedocs.io/en/stable/tutorial/20_recipes/002_multi_objective.html) separates objectives and notes the effect of too many objectives on front membership. [pymoo's problem definition](https://pymoo.org/getting_started/part_1.html) separates objectives from constraints. The scores here are an RSI reporting convention.
