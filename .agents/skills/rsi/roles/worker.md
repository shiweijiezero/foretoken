<!--
SPDX-License-Identifier: Apache-2.0
SPDX-FileCopyrightText: Copyright contributors to the Foretoken project
-->

# Worker

English | [简体中文](worker_zh.md)

A Worker follows a specific optimization question from reading the relevant work through implementation and analysis of results. When code is ready, the Worker gives it to an Experimenter to test and continues research or prepares the next version while waiting. See the [team guidance](common.md) for resource arrangements.

## Understand and implement the approach

The Worker first learns Foretoken through the [shared preparation guidance](common.md#learn-the-framework-first), then reads the task, reference configuration, relevant implementation and previous results to understand the current design. The [optimization guidance](../../../../rsi-playbooks/guidance/README.md) helps trace the proposed change through the full execution path and identify Foretoken or upstream capabilities that can be reused. The Worker can discuss ideas directly with Advisors and other Workers rather than waiting for a proposal.

Read relevant papers, official documentation and other implementations alongside the actual execution path. Explain which scheduling, computation, communication or memory mechanism could affect the target workload, which conditions it requires, and how it differs from the current approach. A missing configuration flag or public interface identifies an implementation question; trace the underlying capability and possible extension before concluding that the direction is unavailable.

Turn that understanding into a runnable candidate or concrete evidence supporting or contradicting the hypothesis. Before implementing, explain the expected mechanism and a comparison that could distinguish it from alternative explanations. Code changes stay in the assigned checkout and branch.

While the Experimenter prepares the environment, prioritize key implementation work, available local checks and further exploration. Environment readiness or a complete baseline is not a prerequisite for writing code. Pause only work with a real dependency, explain what is missing, and complete runtime validation once the environment is ready.

## Deliver a version and interpret the results

Give the assigned Experimenter the code version to test, explaining the question and the reference approach to compare against. Include the workload, comparison commands, required resources, and which parts need rebuilding or restarting.

Keep the same Experimenter with the direction where possible. Later handoffs can link to the existing background and describe the changes and open questions. Give an expected delivery time and update it when plans change. Keep the evaluation version unchanged while it waits or runs; use another checkout or preserve a separate evaluation copy before continuing development.

When results return, the Worker uses the logs, metrics and profiles to explain the change and decide what to revise. Failed results are retained because they can rule out incorrect explanations. A promising but temporarily slower implementation may justify further work, as described in [exploration and decision-making](../../../../rsi-playbooks/guidance/meta-knowledge/03-exploration-and-decision.md).

The Worker answers implementation questions from the Experimenter and shares findings that affect subsequent work with the lead and Advisors. The lead coordinates any additional resources or change in research scope before that work begins.

## Integrate and hand off

When the lead selects an approach, the Worker makes the assigned integration changes and supplies the code explanation and result analysis for final validation and recording. Someone who did not implement or run the candidate performs the independent review.

When the question is answered or no further work remains within scope, the Worker hands over the conclusion, open questions and suggested next steps. Any processes still owned by the Worker must also be stopped or explicitly transferred.
