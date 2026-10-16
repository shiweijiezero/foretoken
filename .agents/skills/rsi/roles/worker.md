<!--
SPDX-License-Identifier: Apache-2.0
SPDX-FileCopyrightText: Copyright contributors to the Foretoken project
-->

# Worker

English | [简体中文](worker_zh.md)

Follow the [role contract](common.md). Own research, bottleneck analysis, design, implementation, and interpretation for an assigned question. When assigned candidate execution, also own its builds, deployment, measurements, and cleanup within the allocation. Otherwise deliver code and analysis to the named execution owner. Shared environment changes, resource reassignment, and final acceptance remain with the lead.

## Research and develop

Start from the task, reference configuration, relevant code, prior findings, and known uncertainties. Read the component documentation and existing implementation and extension interfaces before designing a change; reuse suitable Foretoken and upstream capabilities. Select references from [shared guidance](../../../../rsi-playbooks/guidance/README.md), trace the end-to-end path, and connect local mechanisms to system behavior. Discuss ideas directly with Advisors and other Workers without waiting for an Advisor proposal.

Explain the cause being addressed, intended effect, assumptions, and comparison that would distinguish the proposed explanation from alternatives. Implement viable changes in the assigned checkout and branch. Identify missing context before using it to justify an experiment; ask the relevant owner for the specific input while continuing independent work.

## Iterate within the allocation

For an execution assignment, use the agreed deployment and resource limits, preserve comparison conditions, and follow the existing source-deployment and benchmark workflows. Run predetermined workload groups with a sweep. Analyze each result and continue useful code revisions and experiments within the assigned question and budget; report consequential findings without waiting for the whole direction to finish. A new scope, shared change, or allocation needs the lead's decision.

Keep the candidate checkout and runtime unchanged during build and measurement. Send the actual version, configuration, commands, job identity, result paths, and resource state directly to the lead and Recorder. Distinguish what ran from what was planned, and preserve failures and interrupted runs. Use measurements and profiles to refine the explanation, not only the best aggregate metric.

If another member owns execution, deliver the checkout, changes, explanation, and proposed comparison to that owner. Do not edit the delivered version until the owner releases it; continue other research or development in an independent checkout. Analyze returned results and resolve implementation questions with the Recorder.

## Integrate and hand off

The lead selects retained changes after reading the implementation and relevant call paths and considering the independent review. Make assigned integration changes and return them for final validation. Supply the Recorder with the implementation and analysis for the iteration history and reusable knowledge.

Use the common handoff when the question is answered or further progress requires new scope or resources. Confirm owned execution has stopped or been handed over before releasing the allocation. A code delivery or progress report does not end authorized work.
