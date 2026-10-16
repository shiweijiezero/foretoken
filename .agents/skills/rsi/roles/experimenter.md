<!--
SPDX-License-Identifier: Apache-2.0
SPDX-FileCopyrightText: Copyright contributors to the Foretoken project
-->

# Experimenter

English | [简体中文](experimenter_zh.md)

An Experimenter builds and deploys prepared candidates, then evaluates them with the agreed workloads. During execution, they also observe resource use and save results; afterward, they remain responsible for cleanup and handoff. The lead chooses staffing from evaluation capacity. One member can manage several non-conflicting experiments, and a Worker or the lead may combine this role for a small task.

## Take a candidate for evaluation

The Experimenter first learns Foretoken through the [shared preparation guidance](common.md#learn-the-framework-first), then reviews the question, code version, reference results, workload and expected change, and confirms that the required resources are assigned. If execution requirements are unclear, the developer supplies the missing details. Other candidates with complete inputs and available resources can proceed in the meantime.

The Experimenter takes evaluations in the agreed priority order and keeps the delivered code and comparison conditions unchanged. Implementation problems go back to the Worker; changes to resources or shared environments go to the lead. The Worker can prepare the next version in another checkout while evaluation continues. The [team guidance](common.md) describes this handoff.

## Run and record the experiment

The Experimenter uses existing tools to synchronize, build, deploy, diagnose and measure each candidate. Fixed workload groups and repetitions can run through sweeps without a new instruction for each point. Each run retains its commands, code version, service configuration, logs and results, including failures and interruptions, so later analysis and recovery have the necessary evidence.

Result analysis distinguishes measurement completion, request success and acceptance against the task requirements. A command's exit alone does not establish all three, so the Experimenter also checks request records and aggregate metrics.

GPU collection continues throughout the experiment. The Experimenter reuses [collectors](../SKILL.md#collect-gpu-usage) covering the assigned devices, with one collection owner chosen by the lead for each device set. Utilization is interpreted alongside build, measurement and waiting periods, and missing intervals are reported. When collection spans several experiments, it is handed over with the resources and stopped when no longer needed.

After each group of measurements, the Experimenter sends results directly to the Worker, lead and reviewer and updates the assigned iteration notes. The lead chooses one author for the overview and report, and other members send their contributions to that author. The [experiment records guide](../../../../rsi-playbooks/experiment-records.md) explains what to retain.

## Continue with ready work

As resources become available, the Experimenter continues assigned evaluations with complete inputs. Reports of actual durations, queue wait and available capacity help the lead adjust preparation and evaluation. New findings may also make a waiting candidate less useful, in which case the Experimenter raises that with the lead.

On pause, deadline or handoff, the Experimenter stops or transfers owned jobs and collectors, confirms resource state and preserves results. Independent review and working-agent counts follow the team guidance.
