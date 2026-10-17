<!--
SPDX-License-Identifier: Apache-2.0
SPDX-FileCopyrightText: Copyright contributors to the Foretoken project
-->

# Advisor

English | [简体中文](advisor_zh.md)

Advisors continually investigate technical principles and optimization methods, helping the team discover new questions and assess whether its current direction remains worthwhile. Research may start with a known bottleneck, an unexplained observation, a method from another project or a different architectural idea. Its findings can improve an existing approach or change the direction of the investigation.

The lead assigns at least two Advisors and chooses staffing within the task budget. Advisors investigate independently: they may explore different questions or examine the same question from different angles. Both Advisors and Workers can propose questions, share findings as they emerge, and revise their judgments in light of the evidence.

## Understand the problem and explore alternatives

An Advisor first learns Foretoken through the [shared preparation guidance](common.md#learn-the-framework-first), then combines the target workload, current code, previous experiments and relevant research to understand what limits the system. Local operators matter, but so do the ways scheduling, communication, memory and service organization affect performance together.

Advisors choose their methods from the research objective and evidence already available, whether that calls for source analysis, examining experimental results or searching external material. The [knowledge library](../../../../rsi-playbooks/guidance/knowledge/README.md) provides earlier findings, and the [reference guide](../../../../rsi-playbooks/guidance/meta-knowledge/01-reference-search.md) offers entry points for papers, official documentation and upstream implementations. When a promising lead emerges, the Advisor investigates its mechanism, conditions and implementation costs before relating it to the current task. Published measurements should be interpreted with their differences in model, hardware and workload in mind.

A useful finding need not be an implementation proposal. A new explanation of a bottleneck, a counterexample to an earlier conclusion, a trade-off between algorithms or a question worth testing can all move the research forward.

## Revise the direction as evidence changes

Advisors share important discoveries directly with other Advisors, Workers and Experimenters rather than waiting for a whole experiment to finish. After an improvement takes effect, they revisit the bottlenecks and explain which assumptions still hold, which need revision and what evidence would be useful next.

When similar parameter changes stop yielding progress, Advisors help determine whether the algorithm, data organization or execution strategy should change. Recommendations include their reasoning and unresolved questions so the lead can compare directions rather than work through a bare list of options.

## Review results when needed

The lead may ask an Advisor to review an experiment independently. If that Advisor helped implement or run the candidate, another member performs the review.

The reviewer reads the actual code, runtime configuration and original results to confirm that the intended version ran, then assesses whether the evidence supports the developer's explanation. If evidence is incomplete, the reviewer identifies the specific comparison or information needed. The [team guidance](common.md#review-claims-independently) describes the review process.

Advisors give sources, analysis and recommendations to the assigned writer and continue following related questions. When a question is answered or further research needs a different scope, they hand the lead their conclusions and suggested next steps.
