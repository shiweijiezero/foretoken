<!--
SPDX-License-Identifier: Apache-2.0
SPDX-FileCopyrightText: Copyright contributors to the Foretoken project
-->

# Working as an RSI team

English | [简体中文](common_zh.md)

The root agent establishes the objective, fixed requirements, original baseline, total budget and initial team. Members then own research, implementation and experiments within that authority; fresh Evaluators own independent review. Once the Agent Scheduler is running, the root observes records, checks progress and resource/research boundaries, reminds owners about concrete gaps and briefs the user; it does not dispatch every stage or relay routine messages. The observation and user-update rule is defined once in the [RSI skill](../SKILL.md); members do not wait for root messages. If the scheduler is unavailable or a lease remains stuck, the root uses native follow-up only to repair that scheduling failure. Routine decisions and deliveries go directly to their consumers.

## Learn the framework first

Read the [project README](../../../../README.md), architecture guidance and documentation for the affected path, then the task, current retained configuration and relevant experiment records. Trace producers and consumers before changing behavior. Reuse established context and applicable evidence; refresh what the current question or changed version requires.

## Assign complete questions

Own a research question through investigation, implementation and interpretation, rather than asking for a new assignment after each stage. Choose the next hypothesis, adjust stage estimates and continue, revise or stop within the task's objective and total budget. Important mechanisms deserve implementation time even when smaller changes are ready.

Keep the question, known findings, code, comparison conditions and expected evidence in the existing iteration notes. Submit complete questions or informative stages through [continuation](../continuation.md). Available qualified members claim ready work; familiarity with a topic or environment helps reuse context without fixing Worker–Experimenter pairs or reserving work for a busy member. Explicitly cover responsibilities without a dedicated role. Respect the task's role counts, record real dependencies and do not invent experiments to keep members busy.

All new research and implementation start from the latest validated retained version applicable to the task and workload. Record the actual parent and delta in the iteration notes. If that parent changes during preparation, carry over the relevant changes and validate their affected behavior; preserve the originals of a run already in progress. Use an older base only for a stated reproduction or attribution question. When a branch helps, integrate it with the latest retained configuration and measure the combination; two isolated gains do not establish their combined effect. The original baseline remains the comparison reference.

Role counts do not require a fixed role ratio or pairing. If no member can execute required work, record that capacity gap while independent work continues. Every adoption review belongs to a fresh Evaluator check; without that check, adoption remains pending. The root does not silently fill missing roles.

## Share findings and keep working

An Explorer gives useful research directly to the implementing Worker; the Worker supplies a runnable candidate for an available Experimenter; the executor returns results directly to the owner and records the original paths for the next Evaluator. Review findings go to the person who can act on them. Use existing notes for shared background and send only the conclusion, changed information and precise paths. Routine updates need no acknowledgement.

When a delivery makes a waiting consumer ready, use native follow-up to resume that consumer. A scheduler-reported idle thread is a recovery case, not a normal member state. A message alone does not wake an idle member. Publish work that any qualified member can take through continuation instead of preassigning it to someone busy. Continue independent work while a real dependency is pending.

Give each executable stage one primary owner. Other members use the saved evidence for distinct questions, while the next Evaluator checks the complete claim before retention.

Evaluator findings go directly to the responsible owners. They act on the evidence within their authority and record the outcome in the affected notes. Only a change beyond the user's authorized objective, resource budget or fixed conditions needs new authorization through the user entry point.

## Prepare candidates while evaluations run

Supply the candidate checkout and revision, question, reference, commands, workload, resources and required rebuilds. Keep the delivered source unchanged through build and measurement; use another worktree when development overlaps evaluation.

Share the installed platform, compatible images, model files and reusable caches. Isolate each candidate's source, writable build state, deployment instance, runtime cache and allocated devices. Multiple worktrees can use the same prepared platform concurrently. Compile model and native dependencies on the server using existing compatible images and caches; lightweight local analysis is appropriate.

One executor owns each running experiment through results and cleanup. A change of executor transfers the same job and original artifacts; inspect the existing process before restarting it. Preserve context where useful without requiring a permanent pairing or a global experiment queue.

## Coordinate resources and environments

Experimenters coordinate only the resources their runs actually share. Use existing Kubernetes requests, scheduling and workload state, together with host observations, to arrange devices and node capacity within the task's aggregate budget. Account for serving, builds, clients and model preparation. Shared images or a platform do not require serializing independent deployments.

Before using devices, check current workloads and authorization; release them when no ready experiment follows. Retain environments, model files and build caches. Prepare and measure concurrently where resources permit, but keep competing work out of a matched measurement window. If an environment fails, its owner repairs the relevant path while independent research continues.

## Return results as work finishes

The executor saves each completed point and its interpretation in its own experiment records, including failures and diagnostics. Preserve original commands, source/configuration, request records and resource observations. Deliver useful results without waiting for an entire matrix. Follow the [report workflow](../../../../rsi-playbooks/experiment-records.md#maintain-the-user-facing-report) to aggregate those records through the existing plot entry point; there is no central writer copying results between members.

The research owner interprets the evidence and chooses the next informative work. Reuse comparable completed points and applicable validation. Do not automatically rerun a control for every candidate, repeat control–candidate–control sequences or expand every question into a full matrix; select a repeat only to answer a concrete uncertainty. Task-specific restrictions on measurement take precedence.

## Review claims independently

Before retaining an approach, a fresh Evaluator checks the changed code, effective configuration and original evidence. Reuse reviewed background and validation whose conditions still apply. Investigate an important affected behavior with the smallest meaningful comparison; quality scoring is needed only when output impact and existing evidence warrant it.

The Evaluator records the supported scope and any concrete remaining issue. The research owner fixes or stops the candidate, or retains it when the task's requirements and assessment are satisfied. Integration follows the latest retained configuration and validates the resulting combination. These decisions require no routine root approval.

## Hand off and resume

Finish a stage with its result and publish the next useful work or genuine dependency through [continuation](../continuation.md). A finding or review completes an item, not the continuing research responsibility; research owners choose the next question even when no producer has queued one. An available member claims ready work directly. The producer resumes a known waiting consumer, or a scheduler-reported idle thread while repairing scheduling. Neither stage completion nor a soft time estimate ends an open research responsibility; the task's hard deadline is unchanged.

On pause, stop or deadline, each owner stops or hands over its jobs and collectors, confirms exit and preserves results. A collector deadline governs only that process; serving and build jobs need their own stop action. On resumption, inspect existing jobs and results before launching replacements.

Use native activity for working-member counts, counting each member once. Waiting, blocked and finished members are not active work, and missing intervals stay unknown. Count Evaluators only during their checks.
