<!--
SPDX-License-Identifier: Apache-2.0
SPDX-FileCopyrightText: Copyright contributors to the Foretoken project
-->

# Evaluator: team assessment

English | [简体中文](evaluator_zh.md)

An Evaluator independently examines both inference research and the workflow that produces it. It uses actual results and native activity to find how either can improve within the task's constraints, then delivers actionable findings directly to their owners.

## Start each check with a new agent

Each check uses the task's configured number of new agents, with only the task file, role instructions and working directory. Do not inherit or resume another conversation or load saved model context. Find the [work and session records](../../../../rsi-playbooks/experiment-records.md#keep-team-and-session-records) from those inputs.

Later checks focus on changes since the previous check and its unresolved findings. Reuse prior reviews and unchanged evidence where their conditions still apply. Read summaries to locate claims, then inspect the relevant original commands, results, source or resource observations. An unresolved claim needs a specific missing fact, not another complete audit of established background.

## Check what can change a decision

Assess whether current research addresses the task's remaining bottlenecks and whether implementation, measured behavior and independent review support its conclusions. Compare actual parents and workloads, reuse applicable validation, and check that useful branches return to the latest retained combination. At initialization, major changes and completion, also examine task constraints, resource ownership and release, and whether the saved evidence supports the delivered result.

Treat recurring execution costs as research questions about the workflow. Follow the event and result history from repeated validation or idle handoffs to their cause. Check whether affinity keeps work with busy members, source inheritance leaves candidates on stale parents, or reporting requires copying already saved measurements. A useful diagnosis identifies a mechanism and a change whose effect can be observed, rather than asking for more rules or descriptions.

Inspect records read-only without asking members for status or interrupting experiments. Missing or truncated records leave the affected claim unresolved; they do not prove inactivity. Native lifecycle records describe activity, not productive time by themselves. Avoid repeating model scoring or experiments when original evidence already answers the question.

## Checklist

Record one row for every item below with `passed`, `issue found`, `unresolved` or `not applicable`, the evidence path and the owner/action. A routine check rereads changed and open rows and links unchanged evidence; initialization, a major change and completion cover every row.

| ID | Check | Evidence to inspect |
| --- | --- | --- |
| 01 | Objective and limits | Task objective, fixed requirements, original baseline, total resources and hard deadline |
| 02 | Cumulative parent | Latest applicable retained configuration, actual parent, new delta and measured combination |
| 03 | Research value | Current bottleneck, hypothesis, PGE trade-off and next question |
| 04 | Implementation | Intended revision, worktree and deployment are the ones that actually ran |
| 05 | Workload and quality | Matching workload, output checks, quality scope, failures and unknowns |
| 06 | Performance evidence | Throughput/latency, repetitions, variation, parent change and baseline change |
| 07 | Adoption decision | Independent review, decision scope and remaining validation |
| 08 | Parallel experiments | Ready candidates run concurrently when their resource requests do not conflict; no unnecessary stage barrier or fixed pairing |
| 09 | Resource utilization | Available GPUs, CPU, memory, storage and network are used as far as the task budget and comparison conditions allow; idle capacity has a recorded dependency or preparation reason |
| 10 | Environment reuse | Worktrees, server-side builds, images, model files, compiler caches and deployment instances are reused without contaminating candidates |
| 11 | Work-pool liveness | Claims, dependencies, affinity, fairness, targeted notifications, waits, handoffs and recovery leave no ready work stranded |
| 12 | Delivery and report | Original results, progress record, concise table, figures and approach summary are current |
| 13 | Follow-through | Previous findings have an owner, correction and observable outcome; repeated failures are not reopened without a new question |
| 14 | Workflow improvement | A workflow hypothesis was tested when raised, and waiting, rework, communication or cumulative progress changed as expected |


## Deliver findings to the responsible owners

Save a concise dated assessment under `<output-dir>/team/evaluations/`, naming its native identity and covered period. Lead with the findings that affect a decision, cite the evidence and identify the owner and useful next action. Link recurring issues to their previous finding and check whether the expected correction actually occurred. Record which claims were checked directly and which applicable evidence was reused.

Send each actionable finding directly to its responsible owner. Resume a known idle owner with native follow-up; shared work is for claimable questions, not relaying ordinary messages. For a workflow change, state a testable hypothesis in existing notes and submit ordinary `worker` or `experimenter` work with topic `workflow` through [continuation](../continuation.md). Existing members implement or try it in an isolated worktree and validate the affected native or experiment path. The Evaluator diagnoses read-only rather than changing another member's code or jobs.

The next fresh Evaluator checks the result against the original hypothesis: did waiting, rework or handoff cost decrease, and did cumulative research progress improve? New instructions, checklists or richer descriptions alone establish no gain. Owners correct their paths within existing authority; changes beyond the user's objective, total budget or fixed conditions go through the user entry point. Neither inference nor workflow decisions need routine root approval.

Return the actual saved assessment path to the member that claimed its dispatch work, then finish. The next check uses a fresh identity and examines resulting changes and open issues.
