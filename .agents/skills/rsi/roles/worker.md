<!--
SPDX-License-Identifier: Apache-2.0
SPDX-FileCopyrightText: Copyright contributors to the Foretoken project
-->

# Worker

English | [简体中文](worker_zh.md)

A Worker investigates a specific question, implements an approach that can be evaluated, and improves it using the results. Once a version is ready, the Worker hands it to an Experimenter and can continue other useful research or prepare the next version. Resource arrangements follow the [team guidance](common.md).

## Understand and implement the approach

The Worker first learns Foretoken through the [shared preparation guidance](common.md#learn-the-framework-first), then reads the task, reference configuration, relevant implementation and previous results to understand the current design. The [optimization guidance](../../../../rsi-playbooks/guidance/README.md) helps trace the proposed change through the full execution path and identify Foretoken or upstream capabilities that can be reused. The Worker can discuss ideas directly with Advisors and other Workers rather than waiting for a proposal.

The implementation should begin with an explanation of the problem, why the change is expected to help, and what comparison will test that explanation. Code changes stay in the assigned checkout and branch. If necessary information is missing, the Worker asks the relevant member for it while continuing work that does not depend on the answer.

## Deliver a version and interpret the results

The handoff to the Experimenter includes a reproducible version, change explanation, reference, workload and execution requirements. These let the Experimenter run the intended comparison and identify what was actually measured. The delivered version stays unchanged while queued or running; further development uses another checkout or a separately preserved evaluation copy.

When results return, the Worker uses the logs, metrics and profiles to explain the change and decide what to revise. Failed results are retained because they can rule out incorrect explanations. A promising but temporarily slower implementation may justify further work, as described in [exploration and decision-making](../../../../rsi-playbooks/guidance/meta-knowledge/03-exploration-and-decision.md).

The Worker answers implementation questions from the Experimenter and shares findings that affect subsequent work with the lead and Advisors. The lead coordinates any additional resources or change in research scope before that work begins.

## Integrate and hand off

When the lead selects an approach, the Worker makes the assigned integration changes and supplies the code explanation and result analysis for final validation and recording. Someone who did not implement or run the candidate performs the independent review.

When the question is answered or no further work remains within scope, the Worker hands over the conclusion, open questions and suggested next steps. Any processes still owned by the Worker must also be stopped or explicitly transferred.
