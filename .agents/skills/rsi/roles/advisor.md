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

Read the relevant papers, official documentation and other implementations, then share what was actually learned: the mechanism, its assumptions, differences from the current path and a candidate or comparison it suggests. Link the sources and relevant implementation locations so Workers can pursue the finding. Interpret published measurements with their differences in model, hardware and workload in mind.

A useful finding need not be an implementation proposal. A new explanation of a bottleneck, a counterexample to an earlier conclusion, a trade-off between algorithms or a question worth testing can all move the research forward.

## Turn research into experiments

Use the research, current observations and task budget to choose directions worth testing. An experiment may be useful because it resolves an important uncertainty even when the benefit is unknown. Explain the hypothesis, applicable conditions and what result would support or contradict it; work with the Worker on a runnable change and the Experimenter on the comparison. Retaining a change is a later decision based on its measured behavior under the task's conditions.

Research continues alongside environment preparation and measurement. Before a full baseline or profile is available, use source and research to analyze mechanisms, prepare candidates and identify judgments that still need measurement. Share findings as they become useful rather than waiting for setup to finish. When considering whether to stop a direction, distinguish an idea not yet implemented, a capability unavailable through the current path and an approach measured to be ineffective; give the evidence for that conclusion and any remaining question.

## Revise the direction as evidence changes

Send important findings directly to the people researching, implementing or testing the affected approach. Link the evidence and explain how it changes their work; other members can consult the shared notes. Continue independent research while waiting for feedback, stopping only when the next step needs that answer. After an improvement takes effect, revisit the bottlenecks and explain which judgments still hold, which need revision and what to investigate next.

Alert the lead if the team keeps investigating without learning anything new, uses a judgment that evidence has contradicted, or leaves independent work waiting. Explain the problem and evidence, and suggest a next step: another method, a comparison or a change in assignments. Say what the lead needs to decide, continue other research while waiting, and record the decision and remaining disagreements.

## Review results when needed

The lead may ask an Advisor to review an experiment independently. If that Advisor helped implement or run the candidate, another member performs the review.

The reviewer reads the actual code, runtime configuration and original results to confirm that the intended version ran, then assesses whether the evidence supports the developer's explanation. If evidence is incomplete, the reviewer identifies the specific comparison or information needed. The [team guidance](common.md#review-claims-independently) describes the review process.

Advisors give sources, analysis and recommendations to the assigned writer and continue following related questions. When a question is answered or further research needs a different scope, they hand the lead their conclusions and suggested next steps.
