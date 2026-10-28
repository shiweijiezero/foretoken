<!--
SPDX-License-Identifier: Apache-2.0
SPDX-FileCopyrightText: Copyright contributors to the Foretoken project
-->

# 06 | Assess research opportunities

English | [简体中文](06-opportunity-assessment_zh.md)

A fast, easy experiment can have little remaining upside. A difficult implementation can have substantial upside but weak evidence. Compare these dimensions explicitly instead of counting completed experiments or ranking directions by a single score.

The Observer assesses the current portfolio from sources and run records, quantifies the trade-offs and alerts the lead to unresolved decisions. The lead chooses future directions, budgets, owners and review points. Keep the assessment in the existing Observer note and the lead's response in the existing team record.

## Compare the right objects

Use one row per research question and candidate revision, including active, waiting and prepared directions and documented alternatives. Identify the workload, reference, proposed mechanism, current stage and next evidence milestone already described by its owner. If that milestone is missing, report it as unknown rather than inventing an assignment. Update changed rows each round; preserve the dated evidence behind previous judgments.

Distinguish two fronts:

- The measured deployment front contains configurations evaluated under comparable workloads and resource conditions and meeting the task's correctness and quality requirements. Use measured throughput, latency, resource use and other task objectives. Missing required evaluations leave a configuration outside this validated set.
- The research opportunity front compares current estimates of the potential gains and remaining cost of the recorded directions. Feasibility and evidence scores describe the strength of those estimates. Being non-dominated here does not establish a deployable improvement.

New measurements of the same configuration do not replace the task's original baseline. State the reference used for each gain and keep changed protocols or hardware in separate groups. Directions on one front use the same reference configuration and metric definition. Show a prototype-relative remaining gain separately; convert it to that common reference before comparing opportunities. For a task requiring several workloads, preserve the gain vector: a large gain in one workload must not hide a regression in another. Use an aggregate only when the task already specifies its weights.

## Record values before scores

For every estimate, identify the author, observation time, source, assumptions and range. Use `U` when evidence is missing, and `N/A` when a dimension does not apply. An unknown value is not zero. Scores use whole numbers from 0 to 4; when the value spans bands, report the score range as well. They are descriptive scales, not probabilities or acceptance thresholds. Keep the same anchors within a task; if the lead changes them, rescore the compared rows and explain why.

### Gain and headroom: G

Report measured gain and estimated remaining gain separately, by workload and metric. For throughput, gain is `100 × (candidate / reference − 1)` percent; for latency reduction it is `100 × (1 − candidate / reference)` percent. Preserve negative values, output lengths and measurement variation. The current slowdown of a prototype is not its proposed future gain; explain the specific unresolved change supporting any larger estimate.

Use critical-path cost to bound headroom. If a component occupies fraction `f` of the relevant elapsed time, is accelerated by factor `s`, and adds overhead `a` as a fraction of the original time, the simplified speedup is `S = 1 / (1 − f + f/s + a)`. Its throughput gain under fixed work is `100 × (S − 1)`; its elapsed-time reduction is `100 × (1 − 1/S)`. Apply this only to the measured path and assumptions. Summed GPU kernel durations can overlap and are not automatically `f`; a latency estimate is not automatically a serving throughput estimate.

For example, eliminating a component occupying 4% of elapsed time has an ideal speedup ceiling of 4.17%, before added costs. A 2× speedup of that component gives only 2.04%. This bounds a local optimization's potential even when its kernel benchmark looks impressive.

The default gain bands below summarize each metric separately. The lead may choose different anchors for the task before comparison. A 1% improvement can still matter at production scale; the bands do not mandate stopping it.

| G | End-to-end gain or estimated gain interval |
| --- | --- |
| 0 | Zero or negative; keep the actual signed value |
| 1 | Greater than 0% and less than 5% |
| 2 | At least 5% and less than 10% |
| 3 | At least 10% and less than 20% |
| 4 | At least 20% |

Keep both achievable gain estimates and ideal ceilings. A ceiling alone does not supply an expected gain or a lower bound. Apply the same bands to measured and projected values, label them separately, and do not replace missing workload estimates with the best observed workload.

### Theoretical feasibility: F

F assesses the proposed mechanism under the task's precision, semantics, hardware and resource constraints. It is distinct from current environment readiness.

| F | Evidence anchor |
| --- | --- |
| 0 | The stated mechanism contradicts a required invariant or a demonstrated bound |
| 1 | The mechanism is described, but its essential dependency or correctness argument remains unresolved |
| 2 | Source or analytical reasoning supports the path; major integration assumptions remain |
| 3 | The critical path, dependencies and added costs are accounted for, with a plausible implementation under the task constraints |
| 4 | A representative implementation demonstrates that mechanism and the required invariants; remaining uncertainty concerns its scope or benefit |

Name the assumptions for F1–F3 and the supporting execution for F4. A bug in the current prototype does not prove every implementation of the idea infeasible. Likewise, a published method or available flag is not proof that the required path works in this task.

### Evidence strength: E

E measures confidence in the stated gain or failure claim, not confidence that the direction will succeed. A well-reproduced negative result can score E4 with G0. Report which claim is supported; do not use a component's E score for an unmeasured service gain.

| E | Evidence anchor |
| --- | --- |
| 0 | No supporting evidence for the stated claim yet |
| 1 | Analytical argument, source inspection or results from another setting |
| 2 | Relevant local control or component measurement; service extrapolation remains |
| 3 | A matched service comparison covers the stated condition and required checks, but repeatability or broader coverage is incomplete |
| 4 | Repeated representative comparisons support the claim, its variation and required correctness/quality across the claimed scope |

Report sample counts, repetitions, observed ranges and unresolved confounders. A single run cannot establish a noise floor or a calibrated success probability. Preserve uncertainty intervals with their provenance: repeated min/max, statistical interval or engineering estimate are different objects.

### Cost: C

Keep spent and remaining costs separate. Spent cost supports reflection; only additional cost belongs in a future opportunity comparison. Record wall hours, GPU-hours actually attributable to the task and Agent working hours, with waiting and preparation separated where evidence permits. Do not add concurrent wall intervals or equate an open agent turn with productive work. Token usage or currency is optional when the harness provides reliable attribution and prices.

Estimate the additional implementation, environment preparation, measurement and analysis needed to reach the named evidence milestone. Show the milestone's scope: a ten-minute component probe cannot be compared as if it delivers the same result as a complete service validation. Also show remaining cost to task-level validation when the near-term milestone is only a probe.

For each resource with a defined remaining budget `B`, score its estimated additional cost `c` using `r = c/B`. All rows in one snapshot use the same observation time and budget. Score ranges follow cost ranges. For wall time, `B` is time to the task deadline; GPU/Agent budgets use only explicitly available allocations, not the theoretical maximum of busy shared hosts.

| C (higher is less costly) | Fraction of the remaining resource budget |
| --- | --- |
| 4 | `0 ≤ r ≤ 0.05` |
| 3 | `0.05 < r ≤ 0.15` |
| 2 | `0.15 < r ≤ 0.35` |
| 1 | `0.35 < r ≤ 1` |
| 0 | `r > 1` |

Give `C_time`, `C_gpu` and `C_agent` separately when their budgets exist. With no budget for a dimension, keep its cost in native units and mark the normalized score `U`. A finite time estimate beyond the deadline is C0; an unestimated duration is U. The lead decides whether a smaller milestone has value.

### Information gained: I

An experiment can be valuable even when it rules out an optimization. Score the information actually gained during the observed period, separately from any anticipated learning described by the research owner.

| I | Evidence anchor |
| --- | --- |
| 0 | Repeats known facts without reducing a named uncertainty |
| 1 | Checks setup or reproduces a symptom; competing explanations remain |
| 2 | Distinguishes a named explanation or establishes a useful bound |
| 3 | Resolves a key causal or correctness uncertainty with a discriminating comparison |
| 4 | Establishes an independently checked mechanism or limitation with demonstrated reuse across relevant cases |

Cite the before/after uncertainty. Code volume, novelty claims, messages and PR counts do not establish I. High I can justify completed research without establishing an inference speedup or unlimited further investment.

## Environment work and opportunity cost

Environment work has no inference G unless inference performance itself was measured. Assess it separately by the blockage it removes and its effect on already identified experiments:

| Uplift score U_env | Demonstrated enabling value |
| --- | --- |
| 0 | No documented consumer or current dependency |
| 1 | Convenience or unconfirmed delay reduction for a named consumer |
| 2 | Measured preparation savings for an executable comparison |
| 3 | Removes a demonstrated blocker for a documented experiment |
| 4 | A verified common repair unblocks multiple documented experiments or prevents a reproduced recurring failure |

Estimate net cumulative preparation time saved as `N × saving_per_use − remaining_repair_and_validation_time`, where `N` counts plausible uses within the remaining task, not hypothetical future projects. This is wall time saved only when the savings lie on the task’s serial critical path; overlapping preparation otherwise needs a schedule-based estimate. Use ranges, include deployment costs and show GPU/Agent cost separately. For a repair whose value is making execution possible, name the blocked result and its opportunity estimate instead of inventing saved minutes. Do not credit the same enabled model gain to both the repair and the optimization.

The Observer reports the period and task-to-date shares spent on environment work, implementation, measurement, analysis and waiting, along with coverage and which tasks were delayed. It highlights growing setup cost with unchanged readiness or weak enabling evidence. Preparation can be necessary; its value is judged through the work it makes possible, not an automatic percentage cap.

## Identify non-dominated opportunities

Pareto dominance means no worse on every compared objective and strictly better on at least one. Use the raw gain vector and additional cost dimensions for the main opportunity comparison. The lead fixes the workload set, milestone scope and resource dimensions before comparing. F, E and I accompany the front as evidence annotations, rather than expanding it until every direction is non-dominated.

Do not sum or multiply the ordinal scores into a priority, or interpret `G × F × E / cost` as expected return. A high-confidence small gain and a less certain large opportunity can both deserve consideration. Missing information remains visible outside any claimed fully comparable set.

For bounded estimates, call A robustly dominant over B only when A's lower gain bound is at least B's upper gain bound for every required benefit, and A's upper cost bound is at most B's lower cost bound for every compared cost, with one strict inequality. This is relative to the stated assumptions, not a statistical guarantee. If the bound inequalities do not establish dominance, leave the relationship unresolved. Touching bounds can satisfy the comparison when another dimension is strictly better; unknown values or incompatible conditions cannot establish dominance. Coarse score ties do not imply equal raw values. Give the dominating row and dimensions when declaring dominance.

Being on a front does not mean a direction has substantial upside: a very cheap 1% gain may remain non-dominated. Observer findings therefore also describe absolute headroom, regressions, uncertainty and where time has actually been spent. They alert the lead when work concentrates on low-ceiling directions while documented higher-upside questions remain unattended, or when environment costs grow without changing what can run. The lead determines what to do about the trade-off.

### Worked example

These are synthetic estimates for one throughput workload and common reference configuration, the same service-validation milestone and eight remaining wall hours. Other resource costs are assumed equal and within the task's limits solely for this example.

| Direction | Estimated gain | G | F | E | Additional hours | C_time | I observed | Assessment |
| --- | ---: | --- | ---: | ---: | ---: | --- | ---: | --- |
| A: small kernel change | 1–3% | 1 | 4 | 3 | 0.3–0.4 | 4 | 2 | Non-dominated; low absolute ceiling |
| B: execution redesign | 12–18% | 3 | 3 | 2 | 1.6–2.4 | 2 | 3 | Non-dominated; greater upside and cost, weaker evidence |
| C: another similar tuning sweep | 0–1% | 0–1 | 2 | 1 | 3–4 | 1 | 0 | Robustly dominated by A under these assumptions |
| D: unresolved new idea | U | U | 1 | 0 | U | U | 0 | Incompletely assessed; not proven dominated |

A and B illustrate why a front does not choose the next task. If A's ideal ceiling is 3% but B's estimate comes only from a component benchmark, show both limitations; do not advertise B's estimate as achieved. Separately, an environment repair costing 0.8 hours and saving 0.5 hours on each of three documented runs has estimated net cumulative preparation savings of 0.7 hours, and equals task wall time only if those operations are on its serial critical path. That is a preparation benefit, not a throughput gain.

## Observer delivery and lead response

In each check, keep a compact assessment table for the recorded directions with source links, raw values, scores, uncertainties, spent/remaining cost and frontier status. Summarize changes since the previous check, what work produced new information, and which trade-offs or missing estimates need the lead's attention. The Observer does not choose the next mechanism, set budgets, assign members or change task objectives.

The lead responds with its own interpretation of the objective and evidence, the direction to continue, revise or stop, the owner and additional budget, the expected observable result and the time or evidence condition for revisiting it. It explains a choice of a dominated or low-ceiling direction, for example because it is a necessary prerequisite. The next Observer checks that decision against actual work and new evidence. A failed hypothesis need not terminate a research direction automatically, and the user’s success criteria do not change to make a candidate pass.

## References

[Optuna's multi-objective example](https://optuna.readthedocs.io/en/stable/tutorial/20_recipes/002_multi_objective.html) illustrates separate objectives and warns that many objectives can make most trials non-dominated. [pymoo's problem definition](https://pymoo.org/getting_started/part_1.html) separates objectives from constraints. The assessment scores above are an RSI reporting convention, not calibrated probabilities from either tool.
