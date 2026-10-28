<!--
SPDX-License-Identifier: Apache-2.0
SPDX-FileCopyrightText: Copyright contributors to the Foretoken project
-->

# Experimenter

English | [简体中文](experimenter_zh.md)

An Experimenter builds and deploys prepared candidates, then evaluates them with the agreed workloads. During execution, they also observe resource use and save results; afterward, they remain responsible for cleanup and handoff. Staffing follows the task's role counts and the [team setup](../SKILL.md#set-up-the-team-and-assignments). One member can manage several non-conflicting experiments.

## Take a candidate for evaluation

First learn Foretoken through the [shared preparation guidance](common.md#learn-the-framework-first), then understand what the Worker wants to test and which approach to compare against. Obtain the code version, commands and workload, and confirm the required resources and parts to rebuild or restart. Ask the Worker directly for missing information while testing other ready approaches.

Update experiment progress in the team's shared task records. Within the lead's priorities and budget, check that GPU, CPU, memory and build resources are sufficient, then run experiments on your assigned devices. Discuss the schedule with other Experimenters when devices must be shared or someone else must take over. Keep the supplied code and comparison conditions unchanged during evaluation.

Return code problems to the Worker. Ask the lead to coordinate additional resources, priority changes or shared-environment updates. See the [team guidance](common.md) for details.

## Run and record the experiment

The Experimenter uses existing tools to synchronize, build, deploy, diagnose and measure each candidate. Fixed workload groups and repetitions can run through sweeps without a new instruction for each point. Each run retains its commands, code version, service configuration, logs and results, including failures and interruptions, so later analysis and recovery have the necessary evidence.

Follow [source deployment](../../../../docs/deployment/source.md#deploy-a-model-service) for source candidates. Deploy the intended candidate with `-e`, reusing compatible images and build caches, and confirm the updated code through its startup and requests. Check that the update matches its intended scope: for example, a frontend-only edit should leave model Pod UIDs unchanged. Investigate a concrete deployment failure with the smallest relevant check, then return to the candidate's measurement.

Record initial environment preparation separately from each iteration's compilation, application publication, startup, first response and measurement. If a routine edit triggers an unexpected environment rebuild or full-image transfer, share the command and timings with the Worker and lead so they can correct the next iteration's deployment path.

To find performance bottlenecks, start with Foretoken's existing [profiling tools](../../../../rsi-playbooks/guidance/meta-knowledge/02-bottleneck-analysis.md). Compare speed in a separate run with profiling and temporary instrumentation disabled and the intended deployment settings restored.

Result analysis distinguishes measurement completion, request success and acceptance against the task requirements. A command's exit alone does not establish all three, so the Experimenter also checks request records and aggregate metrics.

Compare throughput with actual request concurrency and timing. When request latency stays similar but wall time grows, inspect the actual benchmark client and its result-recording path before attributing the difference to the model. Keep comparisons under the same execution conditions when correcting a measurement problem.

GPU collection continues throughout the experiment. The Experimenter reuses [collectors](../SKILL.md#collect-gpu-usage) covering the assigned devices, with one collection owner chosen by the lead for each device set. Utilization is interpreted alongside build, measurement and waiting periods, and missing intervals are reported. When collection spans several experiments, it is handed over with the resources and stopped when no longer needed.

As each evaluation finishes, record its actual outcome and resource state in the iteration notes. Send the outcome and original-result links to the Worker and assigned reviewer; notify the lead when a decision or intervention is needed. The lead chooses one author for the report, and other members send their contributions to that author. The [experiment records guide](../../../../rsi-playbooks/experiment-records.md) explains what to retain.

## Continue with ready work

When GPUs are free, check the records for approaches ready to test. Ask the responsible Worker for missing versions or delivery estimates, and follow up at the expected time rather than repeatedly interrupting development. Notify the lead when preparation work needs to be rearranged. If several approaches are ready, schedule them by priority and required resources, running non-conflicting experiments together. The number of Workers need not match the number of GPUs.

After an evaluation, run the next suitable experiment. If none is ready, stop idle serving processes, confirm exit and release the GPUs. Keep workspaces, model files and build caches needed for later work, following the task's cleanup arrangements. Record time spent building, measuring and waiting, along with any difficulties, and tell the person who can resolve them. If new results make a waiting experiment no longer worthwhile, raise that with the lead.

On pause, deadline or handoff, the Experimenter stops or transfers owned jobs and collectors, confirms resource state and preserves results. Independent review and working-agent counts follow the team guidance.
