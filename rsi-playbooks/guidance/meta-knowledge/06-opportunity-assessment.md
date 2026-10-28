<!--
SPDX-License-Identifier: Apache-2.0
SPDX-FileCopyrightText: Copyright contributors to the Foretoken project
-->

# 06 | Pareto-Guided Exploration

English | [简体中文](06-opportunity-assessment_zh.md)

Pareto-Guided Exploration (PGE) organizes agentic inference optimization through opportunity assessment, frontier identification, lead decisions and feedback from experiments.

Compare a direction's potential gain with the work it requires, using existing source analysis and experiment records. The Evaluator assesses current evidence and time use; the lead decides directions, owners and budgets. Keep both in the existing observation and team records.

Update changed entries at Evaluator checks. Reuse applicable evidence and unchanged assessments; a score does not require a new experiment or a quality evaluation. Quality checks follow the task's agreed frequency and are needed for changes that may affect outputs when existing evidence is insufficient.

## Start with a comparison

Group measurements by research question; give a candidate its own row when its mechanism, gain or cost materially differs. Identify the workload, reference, candidate revision, current stage, source links and the owner's recorded estimate of remaining work. Missing estimates stay `U` (unknown); use `N/A` for an inapplicable dimension. The Evaluator reports missing decisions to the lead.

For example, these synthetic estimates use one throughput workload, a common baseline, the same service-validation milestone and eight remaining wall hours. Other resource costs are assumed equal and within budget.

| Direction | Estimated gain | Additional hours | Assessment |
| --- | ---: | ---: | --- |
| A: small kernel change | 1–3% | 0.3–0.4 | Low ceiling; no other row dominates it |
| B: execution redesign | 12–18% | 1.6–2.4 | Higher upside and cost; evidence still local |
| C: similar tuning sweep | 0–1% | 3–4 | Dominated by A under these estimates |
| D: unresolved idea | U | U | Incomplete; dominance unresolved |

A and B offer different trade-offs; the front does not select the next task. B's projected gain is not an achieved service result. A preparation repair that costs 0.8 hours and saves 0.5 hours on each of three remaining runs has net estimated savings of 0.7 hours, counted as preparation benefit rather than inference speedup.

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

Link the supporting evidence, scope and remaining assumptions. A prototype bug does not disprove the whole mechanism; a component result does not establish service performance. E includes applicable correctness evidence already available. Code volume, messages and PR counts do not establish I.

### Additional cost: C

Keep spent cost for reflection and additional cost for decisions. Record wall hours, attributable GPU-hours and Agent working hours. Separate preparation and waiting where evidence permits; concurrent wall intervals cannot be added, and open agent turns are not measured productive time. Token or monetary costs require reliable attribution.

Compare costs to the same evidence milestone. A component probe and complete service validation deliver different results; show the remaining task-level cost when the next milestone is only a probe.

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

Keep demonstrated constraint violations outside the feasible front with their counterevidence; unresolved feasibility stays unknown. Report absolute headroom and time allocation as well as front membership: a cheap 1% gain can remain non-dominated. The Evaluator alerts the lead to low ceilings, regressions, weak estimates or growing costs. The lead explains its choice, assigns work and budget, and sets when to reconsider it. Subsequent checks compare that decision with actual work and evidence.

## References

[Optuna's multi-objective example](https://optuna.readthedocs.io/en/stable/tutorial/20_recipes/002_multi_objective.html) separates objectives and notes the effect of too many objectives on front membership. [pymoo's problem definition](https://pymoo.org/getting_started/part_1.html) separates objectives from constraints. The scores here are an RSI reporting convention.
