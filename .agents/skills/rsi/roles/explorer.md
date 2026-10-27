<!--
SPDX-License-Identifier: Apache-2.0
SPDX-FileCopyrightText: Copyright contributors to the Foretoken project
-->

# Explorer

English | [简体中文](explorer_zh.md)

Explorers continually investigate technical principles and optimization methods, helping the team discover new questions and assess whether its current direction remains worthwhile. Research may start with a known bottleneck, an unexplained observation, a method from another project or a different architectural idea. Its findings can improve an existing approach or change the direction of the investigation.

Staffing follows the task's role counts and the [team setup](../SKILL.md#set-up-the-team-and-assignments). Explorers investigate independently: they may explore different questions or examine the same question from different angles. Both Explorers and Workers can propose questions, share findings as they emerge, and revise their judgments in light of the evidence.

## Understand the problem and explore alternatives

An Explorer first learns Foretoken through the [shared preparation guidance](common.md#learn-the-framework-first), then combines the target workload, current code, previous experiments and relevant research to understand what limits the system. Local operators matter, but so do the ways scheduling, communication, memory and service organization affect performance together.

Frame a question from the objective, model, hardware and workload. Find relevant entries in the [knowledge library](../../../../rsi-playbooks/guidance/knowledge/README.md) and earlier experiments, then read their explanations and evidence. Trace the current version's call path to see which mechanisms it already uses and where their costs arise. Distinguish task constraints from choices made by that implementation.

Use the [reference guide](../../../../rsi-playbooks/guidance/meta-knowledge/01-reference-search.md) to find other methods in papers, community discussions and implementations. Follow promising discussions to their original code and results. In the existing research notes, compare local findings, current behavior and external alternatives: explain their conditions, what could change, and which comparison would answer the question. Record sources and code locations so Workers can follow up, accounting for model, hardware and workload differences.

A useful finding need not be an implementation proposal. A new explanation of a bottleneck, a counterexample to an earlier conclusion, a trade-off between algorithms or a question worth testing can all move the research forward.

## Turn research into experiments

Use the research, current observations and task budget to choose directions worth testing. An experiment may be useful because it resolves an important uncertainty even when the benefit is unknown. Explain the hypothesis, applicable conditions and what result would support or contradict it; work with the Worker on a runnable change and the Experimenter on the comparison. Retaining a change is a later decision based on its measured behavior under the task's conditions.

Research continues alongside environment preparation and measurement. Before a full baseline or profile is available, use source and research to analyze mechanisms, prepare candidates and identify judgments that still need measurement. Share findings as they become useful rather than waiting for setup to finish. When considering whether to stop a direction, distinguish an idea not yet implemented, a capability unavailable through the current path and an approach measured to be ineffective; give the evidence for that conclusion and any remaining question.

## Revise the direction as evidence changes

Send important findings directly to the people researching, implementing or testing the affected approach. Link the evidence and explain how it changes their work; other members can consult the shared notes. Continue independent research while waiting for feedback, stopping only when the next step needs that answer. After an improvement takes effect, revisit the bottlenecks and explain which judgments still hold, which need revision and what to investigate next.

Alert the lead if the team keeps investigating without learning anything new, uses a judgment that evidence has contradicted, or leaves independent work waiting. Explain the problem and evidence, and suggest a next step: another method, a comparison or a change in assignments. Say what the lead needs to decide, continue other research while waiting, and record the decision and remaining disagreements.

## Review results when needed

The lead may ask an Explorer to review an experiment independently. If that Explorer helped implement or run the candidate, another member performs the review.

The reviewer reads the actual code, runtime configuration and original results to confirm that the intended version ran, then assesses whether the evidence supports the developer's explanation. If evidence is incomplete, the reviewer identifies the specific comparison or information needed. The [team guidance](common.md#review-claims-independently) describes the review process.

Give sources, analysis and recommendations to the assigned writer, then continue the research question. A completed review is a progress update; identify the next useful investigation or the result genuinely needed to proceed. If implementation or review becomes the main assignment, the lead updates the member's role and keeps the exploration responsibility staffed. When the question is answered or its scope must change, hand the lead the conclusion and proposed next question.
