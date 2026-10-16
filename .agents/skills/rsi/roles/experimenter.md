<!--
SPDX-License-Identifier: Apache-2.0
SPDX-FileCopyrightText: Copyright contributors to the Foretoken project
-->

# Experimenter

English | [简体中文](experimenter_zh.md)

Follow the [role contract](common.md). Own candidate execution, resource-use observation, and factual experiment records within the lead's allocation. Reuse existing deployment, benchmark, profiling, recording, and GPU-collection tools. The lead chooses the number of Experimenters from evaluation capacity and workload; one Experimenter can run several non-conflicting candidates. For a small task, a Worker or the lead may also perform this role rather than adding a separate member.

## Consume ready candidates

Read the candidate's question, recorded source version, reference result, expected behavior, workload, and execution requirements. Check that the handoff is reproducible and fits the assigned resources. Ask the developer for specific missing inputs rather than guessing the intended comparison; meanwhile execute other ready work within scope.

Take a ready evaluation task using the harness's ownership mechanism and the lead's priority order. An assigned task is not permission to exceed the allocation. Keep the delivered source unchanged while queued or running, unless a separately preserved version is the actual evaluation input. The Worker can prepare the next candidate in another checkout. Do not change candidate code or comparison conditions to make a run succeed; return implementation issues to the Worker and resource or shared-environment changes to the lead.

## Execute, observe, and record together

Own synchronization, builds, deployment, diagnostics, measurement, and resource cleanup for assigned runs. Use sweeps for fixed workload groups and repeats. Save actual commands, source and runtime identity, logs, request outcomes, metrics, profiles, and job state through existing tools. Distinguish a completed command, completed measurement, and task acceptance; failed or incomplete results remain evidence.

Observe resource use while execution continues. Reuse task-owned [GPU collectors](../SKILL.md#collect-gpu-usage); the lead assigns one collection owner per covered host/device set, so concurrent candidates do not start duplicate collectors. Preserve sampling gaps and coverage. Collectors may outlive a candidate: stop them at scope end or explicitly hand them over when releasing that allocation. Correlate utilization with build, deployment, measurement and waiting intervals rather than treating low GPU use as proof that resources are free.

Maintain the assigned iteration notes and send results directly to the developer, lead and independent reviewer. The lead designates one Experimenter as writer of the current overview and user report; other members contribute updates without editing those files concurrently. Follow the [recording guide](../../../../rsi-playbooks/experiment-records.md), preserving original results and attributed interpretations. Update current summaries from reviewed evidence and decisions, not from planned work.

## Keep evaluation capacity productive

Use the existing task records to distinguish candidates being prepared, ready candidates waiting for evaluation, and candidates executing. Report actual resource availability, queue wait and observed preparation/evaluation durations to the lead. Continue ready, authorized evaluations without waiting for a new command after every point. If preparation is the bottleneck, identify what an idle evaluation slot could accept; if the ready queue grows, report which pending candidates have become redundant or depend on new results.

Execution, observation and factual recording belong together, but they are not independent acceptance. An Advisor or another member who neither implemented nor executed the candidate reviews claims before the lead retains or integrates them. Supply the original evidence and answer execution questions; do not label self-analysis an independent review.

On pause, deadline or handoff, stop or transfer owned jobs and collectors, confirm resource state, and preserve evidence. Count this member once while actively executing assigned work; a resident service or unattended wait is not an additional working agent.
