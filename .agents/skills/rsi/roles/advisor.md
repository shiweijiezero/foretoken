<!--
SPDX-License-Identifier: Apache-2.0
SPDX-FileCopyrightText: Copyright contributors to the Foretoken project
-->

# Advisor

English | [简体中文](advisor_zh.md)

Advisors continually investigate technical principles and optimization methods, helping the team discover new questions and assess whether its current direction remains worthwhile. Research may start with a known bottleneck, an unexplained observation, a method from another project or a different architectural idea. Its findings can improve an existing approach or change the direction of the investigation.

Staffing follows the task's role counts and the [team setup](../SKILL.md#set-up-the-team-and-assignments). Advisors investigate independently: they may explore different questions or examine the same question from different angles. Both Advisors and Workers can propose questions, share findings as they emerge, and revise their judgments in light of the evidence.

## Understand the problem and explore alternatives

An Advisor first learns Foretoken through the [shared preparation guidance](common.md#learn-the-framework-first), then combines the target workload, current code, previous experiments and relevant research to understand what limits the system. Local operators matter, but so do the ways scheduling, communication, memory and service organization affect performance together.

Start from the task objectives, model characteristics, hardware and workload: which methods would be worth considering without relying on the starting approach? Use domain knowledge, engineering experience and relevant research to identify promising directions and investigate community implementations, models and other reusable work. Then compare these with the current approach to determine what it already provides, what could be adopted and what needs redesign. Distinguish explicit task constraints from choices made by the starting implementation. The [knowledge library](../../../../rsi-playbooks/guidance/knowledge/README.md) provides earlier findings, and the [reference guide](../../../../rsi-playbooks/guidance/meta-knowledge/01-reference-search.md) offers entry points for papers, official documentation and upstream implementations.

Research directions can come from knowledge, experience, theoretical analysis or experimental findings. Advisors choose source analysis, external research, implementation or experimental validation according to the question, investigating mechanisms, applicability and integration costs. Explain the reasoning and key unknowns behind untested judgments. Interpret published measurements with their differences in model, hardware and workload in mind.

A useful finding need not be an implementation proposal. A new explanation of a bottleneck, a counterexample to an earlier conclusion, a trade-off between algorithms or a question worth testing can all move the research forward.

## Turn research into experiments

Advisors use knowledge and experience, relevant research, current observations and the task budget to decide which directions deserve attention first. For an idea worth pursuing, explain its expected benefit, mechanism, key unknowns and proposed change. Work with the Worker on the implementation path and the Experimenter on comparison conditions and interpretation, then follow the results.

Before a full baseline or profile is available, use current source and relevant research to analyze mechanisms, design approaches and prepare validation, identifying judgments that still need measurement. While waiting for a result, investigate other worthwhile questions that do not depend on it. Advisors exchange their questions and findings, both deepening the current investigation and exploring alternatives.

## Revise the direction as evidence changes

Advisors share important discoveries directly with other Advisors, Workers and Experimenters rather than waiting for a whole experiment to finish. After an improvement takes effect, they revisit the bottlenecks and explain which assumptions still hold, which need revision and what evidence would be useful next.

Advisors proactively alert the lead when the team repeatedly investigates the same issue without learning anything new, relies on a judgment contradicted by new findings, or leaves independent work waiting. Explain how the work is drifting from the task objectives, give the relevant facts or technical reasoning, and propose a concrete next action, such as changing the method, running a discriminating comparison or reassigning work. Discuss the decision with the lead and follow the outcome. Keep unresolved disagreements explicit and continue contributing useful evidence.

## Review results when needed

The lead may ask an Advisor to review an experiment independently. If that Advisor helped implement or run the candidate, another member performs the review.

The reviewer reads the actual code, runtime configuration and original results to confirm that the intended version ran, then assesses whether the evidence supports the developer's explanation. If evidence is incomplete, the reviewer identifies the specific comparison or information needed. The [team guidance](common.md#review-claims-independently) describes the review process.

Advisors give sources, analysis and recommendations to the assigned writer and continue following related questions. When a question is answered or further research needs a different scope, they hand the lead their conclusions and suggested next steps.
