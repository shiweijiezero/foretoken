<!--
SPDX-License-Identifier: Apache-2.0
SPDX-FileCopyrightText: Copyright contributors to the Foretoken project
-->

# 06 | Pareto-Guided Exploration

English | [简体中文](06-opportunity-assessment_zh.md)

Pareto-Guided Exploration (PGE) helps a team pursue important problems with substantial improvement potential through changes to algorithms, operators, dataflow and execution architecture. The lead selects primary directions and funds sustained work, with smaller improvements and independent alternatives progressing in parallel. The Evaluator assesses progress, costs and gains so the lead can adjust the research portfolio.

## Organize research before ranking candidates

Identify the limitations that matter most to the target workload and investigate mechanisms that could change them. Explorers keep developing new ideas; the lead gives the primary questions time for core implementation and informative experiments. Quick improvements can use parallel capacity, while research priority follows importance and potential impact.

Compare existing candidates by their gains and costs. Research on new mechanisms starts from a question, a hypothesis and its key uncertainties. Gain and integration estimates develop through source analysis and prototyping. The lead invests according to the potential of the research direction, giving important untested ideas room to develop.

For example, research questions at different stages can be described as follows:

| Research question | Available evidence | Important uncertainty | Information the work could produce |
| --- | --- | --- | --- |
| Can a different data layout remove repeated materialization? | Intermediate copies occupy a substantial part of the observed path | Whether consumers can use the new layout without conversion overhead | A working core path and an account of traffic saved versus added |
| Can execution be reorganized to overlap exposed stages? | Profiles show waiting and partly serialized work | Which dependencies are essential and which follow from the implementation | A causal dependency model and a runnable alternative schedule |
| Can a different speculation mechanism reduce cost per accepted token? | Drafting and verification costs are recorded across workloads | Whether the proposed change reduces total work rather than acceptance alone | An explanation or prototype that resolves the combined cost trade-off |

A stage can explain a mechanism, expose an important limitation or deliver a working core implementation; service gains are validated through integration. When a prototype is slow or fails, source analysis and measurements help determine whether to improve its implementation or reconsider the hypothesis.

Group results by research question and version, recording the workload, reference, stage, sources and remaining-work estimates. Use `U` for unknown values and `N/A` for inapplicable ones, updating them as evidence arrives. Schedule quality validation according to output impact and the task’s evaluation policy; retained approaches meet its correctness, precision and resource requirements.

## Keep measurements and opportunities distinct

The measured deployment front compares performance and resource use for approaches that meet the task’s correctness and quality requirements. Reuse applicable prior validation and record any pending output checks alongside the speed results.

The research opportunity front compares projected gains and further investment. Preserve the original baseline, use a common reference and metric definition within each group, and separate different hardware or protocols. Show both the remaining opportunity relative to the current prototype and total gain against the common reference. Present each required workload separately, using aggregate values when the task specifies their weights.

Each estimate names its author, time, assumptions, source and range. State whether the range comes from repeated measurements, statistical analysis or engineering estimates, and account for that uncertainty when interpreting gains.

## Describe gain, evidence and cost

Keep raw values first and summarize them with the following 0–4 scores for discussion. Report score ranges when values span bands. Use consistent anchors within a task; explain changes and update all entries being compared.

### Gain and headroom: G

For throughput, gain is `100 × (candidate / reference − 1)` percent; for latency reduction, `100 × (1 − candidate / reference)` percent. Record regressions, output lengths and variation, and label measurements, projected gains and ideal ceilings separately.

| G | Gain per required metric |
| --- | --- |
| 0 | Zero or negative; retain the signed value |
| 1 | Greater than 0%, less than 5% |
| 2 | At least 5%, less than 10% |
| 3 | At least 10%, less than 20% |
| 4 | At least 20% |

Estimate headroom from the relevant critical path. For elapsed-time fraction `f`, local speedup `s` and added overhead fraction `a`, simplified speedup is `S = 1 / (1 − f + f/s + a)`. Under fixed work, throughput gain is `100 × (S − 1)`%; elapsed-time reduction is `100 × (1 − 1/S)`%. Eliminating a component occupying 4% of elapsed time yields at most 4.17% throughput gain; accelerating it 2× yields 2.04%. Measure `f` on the actual critical path, then validate service throughput under the corresponding workload.

### Feasibility F, evidence E and information I

F describes the mechanism under the task's semantics, precision, hardware and resource constraints, separately from environment readiness. E supports a named conclusion: a well-supported negative result can be E4 with G0. I describes information gained in the observed period, separately from future learning anticipated by the owner.

| Score | F: mechanism feasibility | E: evidence for the stated claim | I: information gained |
| --- | --- | --- | --- |
| 0 | Violates a required invariant or demonstrated bound | No supporting evidence yet | Repeats known facts |
| 1 | Mechanism described; essential assumptions unresolved | Analysis, source or another setting | Reproduces a symptom or checks readiness |
| 2 | Analysis supports the path; integration assumptions remain | Local control or component measurement | Distinguishes an explanation or establishes a useful bound |
| 3 | Dependencies and added costs accounted for within task constraints | Matched service comparison; replication or coverage incomplete | Resolves a key causal or correctness uncertainty |
| 4 | Representative execution demonstrates the mechanism and required invariants | Repeated representative comparisons support the claimed scope | Independently checked finding reused in relevant cases |

Link each score to its evidence, scope and remaining assumptions. Lower F or E identifies questions for deeper research; consider both current prototype performance and the opportunities a different implementation could open. E can reuse applicable correctness evidence, and I reflects the questions actually resolved.

### Additional cost: C

Use spent cost for reflection and additional cost to plan further work. Record elapsed hours, attributable GPU-hours and Agent working hours, identifying preparation, waiting and overlapping periods. Derive Agent working time from actual activity; include token or monetary cost when reliable data is available.

Compare costs for a stated outcome, such as component analysis, a core prototype or service validation. Estimate the next stage first and keep unclear integration costs as U, refining them as implementation progresses. Important research can begin with this staged investment.

For a remaining resource budget `B > 0`, score additional cost `c` by `r = c/B`. With an exhausted budget, positive additional cost is C0 and zero additional cost is C4. All rows in a snapshot use the same observation time and budget. Retain raw costs and remaining budgets across checks to explain changes in C.

| C (higher means less costly) | Fraction of remaining budget |
| --- | --- |
| 4 | `0 ≤ r ≤ 0.05` |
| 3 | `0.05 < r ≤ 0.15` |
| 2 | `0.15 < r ≤ 0.35` |
| 1 | `0.35 < r ≤ 1` |
| 0 | `r > 1` |

Give `C_time`, `C_gpu` and `C_agent` only for defined budgets. With no budget, retain raw cost and mark C as `U`. Wall budget ends at the task deadline; GPU and Agent budgets reflect available allocations. Use U for unknown costs and C0 for known estimates exceeding budget.

## Assess environment work separately

Assess environment work through the experiments it enables and preparation time it saves; record G when inference changes are measured. Summarize its enabling value as `U_env`: 0, no documented consumer; 1, convenience or unconfirmed delay reduction; 2, measured preparation savings; 3, a documented experiment unblocked; 4, a verified repair unlocking multiple experiments or preventing a reproduced recurring failure.

Estimate net cumulative preparation savings as `N × saving_per_use − remaining_repair_and_validation_time`, with N limited to plausible remaining uses and deployment included. Savings count as task wall time only on the serial critical path; overlapping preparation needs a schedule-based estimate. Show GPU and Agent cost separately. When a repair removes an execution blocker, identify the experiment and its research value.

The Evaluator uses period and task-to-date costs to report which work preparation enabled, what remains blocked and how this affects primary research, so the lead can adjust the allocation.

## Identify non-dominated opportunities

Pareto dominance means no worse on every compared objective and strictly better on at least one. Compare raw gain vectors and additional cost dimensions under the lead's common workload, milestone and resource scope. F, E and I accompany the raw values to explain the trade-offs.

For bounded estimates, A robustly dominates B only if every gain lower bound of A is at least B's upper bound and every cost upper bound of A is at most B's lower bound, with one strict inequality. Check these conditions using raw values and state the assumptions; leave dominance unresolved when evidence is insufficient.

For example, two paths to the same validation milestone use the same reference: A has gain bounds of 4–6% and additional cost of 3–4 hours; B has 8–10% and 1–2 hours. B dominates A under these assumptions. Update the comparison when a mechanism has a new implementation or new evidence.

Compare the front among candidates that meet the task’s constraints, and keep recording uncertainties and research progress for ideas whose feasibility is unresolved. Directions on the front have different trade-offs; primary research also considers importance and breakthrough potential. The Evaluator checks whether important mechanisms receive sustained ownership, implementation time and experiment access, and reports when small changes, reporting or environment work displace them.

The lead responds by choosing or revising the research portfolio: the primary problems, independent alternatives, smaller concurrent work, owners, stage budgets and expected learning or implementation outcomes. Give important questions sustained attention and return prototype failures or slowdowns to their owners for analysis. Adjust directions based on new understanding, potential gains and the value of further work. Subsequent evaluations compare the allocation and progress with these decisions.
