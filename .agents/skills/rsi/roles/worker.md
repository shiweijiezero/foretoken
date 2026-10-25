<!--
SPDX-License-Identifier: Apache-2.0
SPDX-FileCopyrightText: Copyright contributors to the Foretoken project
-->

# Worker

English | [简体中文](worker_zh.md)

A Worker owns a mechanism question through research, implementation and analysis across iterations. Once a version is ready, the Worker hands it to an Experimenter and continues investigating the question or preparing the next version while evaluation runs. Resource arrangements follow the [team guidance](common.md).

## Understand and implement the approach

The Worker first learns Foretoken through the [shared preparation guidance](common.md#learn-the-framework-first), then reads the task, reference configuration, relevant implementation and previous results to understand the current design. The [optimization guidance](../../../../rsi-playbooks/guidance/README.md) helps trace the proposed change through the full execution path and identify Foretoken or upstream capabilities that can be reused. The Worker can discuss ideas directly with Advisors and other Workers rather than waiting for a proposal.

Read relevant papers, official documentation and other implementations alongside the actual execution path. Explain which scheduling, computation, communication or memory mechanism could affect the target workload, which conditions it requires, and how it differs from the current approach. A missing configuration flag or public interface identifies an implementation question; trace the underlying capability and possible extension before concluding that the direction is unavailable.

Turn that understanding into a runnable candidate or concrete evidence supporting or contradicting the hypothesis. Before implementing, explain the expected mechanism and a comparison that could distinguish it from alternative explanations. Code changes stay in the assigned checkout and branch. Environment preparation proceeds with its owner while the Worker continues research and implementation that does not require the environment; report a specific missing input when it blocks the next step.

## Deliver a version and interpret the results

Give the Experimenter the fixed version, hypothesis, reference and comparison commands, workloads, resource needs and expected rebuild or restart scope. State when the version will be ready, and update that estimate when preparation changes. These inputs let the Experimenter place the candidate in the existing queue and run it when resources fit. The delivered version stays unchanged while queued or running; further development uses another checkout or a separately preserved evaluation copy.

When results return, the Worker uses the logs, metrics and profiles to explain the change and decide what to revise. Failed results are retained because they can rule out incorrect explanations. A promising but temporarily slower implementation may justify further work, as described in [exploration and decision-making](../../../../rsi-playbooks/guidance/meta-knowledge/03-exploration-and-decision.md).

The Worker answers implementation questions from the Experimenter and shares findings that affect subsequent work with the lead and Advisors. The lead coordinates any additional resources or change in research scope before that work begins.

## Integrate and hand off

When the lead selects an approach, the Worker makes the assigned integration changes and supplies the code explanation and result analysis for final validation and recording. Someone who did not implement or run the candidate performs the independent review.

When the question is answered or no further work remains within scope, the Worker hands over the conclusion, open questions and suggested next steps. Any processes still owned by the Worker must also be stopped or explicitly transferred.
