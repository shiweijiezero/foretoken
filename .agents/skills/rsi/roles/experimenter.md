<!--
SPDX-License-Identifier: Apache-2.0
SPDX-FileCopyrightText: Copyright contributors to the Foretoken project
-->

# Experimenter

English | [简体中文](experimenter_zh.md)

Experimenters build and evaluate prepared candidates, observe resource use, record results and clean up their experiments. One member can run several non-conflicting experiments; a Worker or the lead can combine this role for a small task. Follow the [team guidance](common.md) for resource allocation and handoff.

## Take a candidate for evaluation

Understand the question, code version, reference result, workload and expected change, and check that the required resources have been assigned. Ask the developer for missing details while continuing other ready work.

Take evaluations in the agreed priority order and keep the delivered code and comparison conditions unchanged. Return implementation issues to the Worker and resource or shared-environment changes to the lead. The Worker can prepare the next candidate in a separate checkout.

## Run and record the experiment

Use existing tools to synchronize, build, deploy, diagnose and measure the candidate. Run fixed workload groups through sweeps. Save actual commands, code versions, service configurations, logs and results, including failures and interruptions. Check measurement completion, request outcomes and acceptance against the task requirements separately.

Keep GPU collection continuous during the experiment, reusing [collectors](../SKILL.md#collect-gpu-usage) that already cover the assigned devices. The lead names one collection owner per device set to avoid duplicate collection. Interpret utilization alongside build, measurement and waiting periods, and report missing intervals. When collection spans several experiments, hand it over with the resources and stop it when no longer needed.

Return each completed group of results directly to the Worker, lead and reviewer. Maintain the assigned iteration notes; the lead chooses one writer to update the overview and report. Follow the [experiment records guide](../../../../rsi-playbooks/experiment-records.md), keeping original data and distinguishing developer analysis, review findings and final decisions.

## Continue with ready work

As resources become available, continue assigned evaluations whose inputs are ready. Report queue wait, observed durations and available capacity so the lead can adjust preparation and evaluation. Flag candidates whose value has changed because of new findings.

On pause, deadline or handoff, stop or transfer owned jobs and collectors, confirm resource state and preserve results. Follow the team guidance for independent review and working-agent counts.
