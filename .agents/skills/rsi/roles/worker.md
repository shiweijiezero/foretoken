<!--
SPDX-License-Identifier: Apache-2.0
SPDX-FileCopyrightText: Copyright contributors to the Foretoken project
-->

# Worker

English | [简体中文](worker_zh.md)

Workers investigate specific problems, implement improvements and revise them using experimental results. Give prepared versions to an Experimenter for evaluation and continue useful research or development while waiting. Small tasks may combine development and execution; follow the [team guidance](common.md) for resource assignments.

## Understand and implement the approach

Read the task, reference configuration, relevant code and earlier results to understand the current design. Use the [optimization guidance](../../../../rsi-playbooks/guidance/README.md) to trace the change through the full execution path, reusing suitable Foretoken and upstream capabilities. Discuss ideas directly with Advisors and other Workers rather than waiting for a proposal.

Explain the problem, why the change should help and what comparison would test that explanation. Work in the assigned checkout and branch. Ask the relevant member for missing information while continuing work that does not depend on it.

## Deliver a version and interpret the results

Give the Experimenter a reproducible code version, change explanation, reference, workload and execution requirements. Keep that version unchanged while queued or running. Develop the next version in another checkout or first preserve a separate evaluation copy.

Use returned logs, metrics and profiles to explain the outcome and revise the implementation. Retain failed results too. A justified short-term regression can merit further investigation; see [exploration and decision-making](../../../../rsi-playbooks/guidance/meta-knowledge/03-exploration-and-decision.md).

Answer the Experimenter's implementation questions and share findings that affect subsequent work with the lead and Advisors. Changes to resources or research scope are coordinated by the lead.

## Integrate and hand off

When the lead selects an approach, make the assigned integration changes and provide the code explanation and result analysis for final validation and recording. Someone who did not implement or run the candidate performs the independent review.

When the question is answered or no further work remains within scope, follow the team handoff procedure. Stop or explicitly transfer any processes you own.
