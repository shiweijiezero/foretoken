<!--
SPDX-License-Identifier: Apache-2.0
SPDX-FileCopyrightText: Copyright contributors to the Foretoken project
-->

# Experimenter

English | [简体中文](experimenter_zh.md)

An Experimenter builds and deploys prepared candidates, then evaluates them with the agreed workloads. During execution, they also observe resource use and save results; afterward, they remain responsible for cleanup and handoff. Staffing follows the task's role counts and the [team setup](../SKILL.md#set-up-the-team-and-assignments). One member can manage several non-conflicting experiments.

## Take a candidate for evaluation

The Experimenter first learns Foretoken through the [shared preparation guidance](common.md#learn-the-framework-first), then takes the Worker's fixed version, hypothesis, reference and comparison commands, workload, resource needs and expected rebuild or restart scope. Resolve missing execution details directly with the Worker while other ready candidates proceed.

Maintain assigned evaluations in the same task records used by the lead and Workers. Within the lead's priorities and resource budget, independently run ready candidates on the available device groups assigned to you, accounting for GPU, CPU, memory and build capacity. Coordinate with other Experimenters when resources overlap or ownership must transfer. Keep the delivered code and comparison conditions unchanged. Implementation problems go back to the Worker; additional resources, changed priorities or shared environment changes go to the lead. The [team guidance](common.md) describes allocation and handoffs.

## Run and record the experiment

The Experimenter uses existing tools to synchronize, build, deploy, diagnose and measure each candidate. Fixed workload groups and repetitions can run through sweeps without a new instruction for each point. Each run retains its commands, code version, service configuration, logs and results, including failures and interruptions, so later analysis and recovery have the necessary evidence.

Follow the [iteration workflow](../../../../rsi-playbooks/README.md#reuse-the-environment-across-iterations) for source candidates. Deploy the intended candidate with `-e`, reusing compatible images and build caches, and confirm the updated code through its startup and requests. Check that the update matches its intended scope: for example, a frontend-only edit should leave model Pod UIDs unchanged. Investigate a concrete deployment failure with the smallest relevant check, then return to the candidate's measurement.

Record initial environment preparation separately from each iteration's compilation, application publication, startup, first response and measurement. If a routine edit triggers an unexpected environment rebuild or full-image transfer, share the command and timings with the Worker and lead so they can correct the next iteration's deployment path.

For performance diagnosis, follow the [bottleneck analysis guidance](../../../../rsi-playbooks/guidance/meta-knowledge/02-bottleneck-analysis.md), preferring native profiling to ad hoc probes. Keep diagnostic captures separate from performance comparisons, which run without profiling or temporary instrumentation under the target execution settings.

Result analysis distinguishes measurement completion, request success and acceptance against the task requirements. A command's exit alone does not establish all three, so the Experimenter also checks request records and aggregate metrics.

GPU collection continues throughout the experiment. The Experimenter reuses [collectors](../SKILL.md#collect-gpu-usage) covering the assigned devices, with one collection owner chosen by the lead for each device set. Utilization is interpreted alongside build, measurement and waiting periods, and missing intervals are reported. When collection spans several experiments, it is handed over with the resources and stopped when no longer needed.

As each evaluation finishes, record its actual outcome and resource state in the iteration notes. Send the outcome and original-result links to the Worker and assigned reviewer; notify the lead when a decision or intervention is needed. The lead chooses one author for the report, and other members send their contributions to that author. The [experiment records guide](../../../../rsi-playbooks/experiment-records.md) explains what to retain.

## Continue with ready work

When capacity is available, check candidate readiness in the shared records and ask the responsible Workers for missing versions or delivery estimates. Use those estimates to time follow-ups instead of repeatedly interrupting implementation. Report a preparation bottleneck when the lead needs to redirect work. When several candidates are ready, use their resource needs and agreed priorities to keep evaluations moving; the number of Workers need not match the number of GPUs.

After an evaluation, start the next suitable job or stop idle serving processes and release the GPUs after confirming exit. Preserve reusable workspaces, model files and build caches for later iterations, following the task's cleanup arrangements. Record actual build and measurement durations, queue wait and delivery bottlenecks; notify the owner who can address them. If new findings make a queued candidate less useful, raise that with the lead.

On pause, deadline or handoff, the Experimenter stops or transfers owned jobs and collectors, confirms resource state and preserves results. Independent review and working-agent counts follow the team guidance.
