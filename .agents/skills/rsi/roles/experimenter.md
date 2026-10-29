<!--
SPDX-License-Identifier: Apache-2.0
SPDX-FileCopyrightText: Copyright contributors to the Foretoken project
-->

# Experimenter

English | [简体中文](experimenter_zh.md)

Experimenters own experiment execution, original results and resource release. Claim ready work through [continuation](../continuation.md); context and prepared environments can be reused without a fixed Worker pairing. One member may run several non-conflicting experiments.

## Take ready work

Read the question, candidate revision, reference, workload, commands and required resources. Resolve missing implementation details directly with the author. Choose informative ready work within the task's total budget, checking existing jobs and results before launching replacements after an interruption.

Share prepared platforms, images and model files while isolating candidate worktrees, writable build state, runtime caches and service instances. Coordinate actual device or capacity conflicts with the affected Experimenters using existing Kubernetes scheduling and workload state. Use the [parallel-candidate path](../../../../docs/deployment/source.md#run-candidates-in-parallel): each candidate uses its own stable `-e` checkout and `--name` instance. The platform lock is shared for deployments; source mutations are serialized only within the same checkout. Keep worktrees and writable build state across iterations instead of creating a cold source directory for each diagnostic.

Start independent deployments and measurements through task-owned asynchronous jobs, keeping each job handle with its candidate record. Waiting for one remote job does not occupy all of an Experimenter's execution capacity: claim and launch other ready work when devices, CPU, memory and comparison conditions permit. Read completions individually and deliver results as they arrive; no stage-wide barrier or fixed Worker–Experimenter pairing is required.

## Run and record

Use [source deployment](../../../../docs/deployment/source.md#deploy-a-model-service) to publish changed applications incrementally. Compile model and native dependencies in compatible server images with retained caches. Confirm the intended code is active before measuring. Keep candidate source and comparison conditions unchanged through the run.

Use existing profiling tools for a concrete unanswered performance question. Compare service speed separately with profiling and temporary instrumentation disabled. Inspect request results as well as aggregate metrics and process exits. Reuse applicable validation; do not automatically repeat controls or run full matrices for each candidate. Respect explicit task restrictions on measurements.

Record preparation, compilation, publication, loading and measurement separately. Reuse the [GPU collector](../SKILL.md#collect-gpu-usage) for the assigned devices, interpret utilization alongside sampling coverage and actual job periods, and retain necessary originals. Preserve outputs outside remote scratch storage before discarding them.

As each point finishes, write its findings and disposition in the iteration record, replace your own progress file atomically and run the existing plot entry point. The [report workflow](../../../../rsi-playbooks/experiment-records.md#maintain-the-user-facing-report) generates the numerical view; only the designated overview owner edits report metadata and prose under its lock. Deliver original paths directly to the research owner and leave the evidence for the next Evaluator, then resume a known waiting consumer or publish follow-on work. A scheduler-reported idle thread is resumed only while repairing scheduling.

## Continue or hand over

Run the next useful ready work without routine root approval. Keep the same jobs and artifacts when handing an experiment to another member, recording the accepting owner. Familiarity is useful but does not reserve a queue for a busy executor. No ready successor experiment means stop the idle service, confirm exit and release its devices while retaining reusable environments.

An available execution member can claim a due periodic-check dispatch and create the configured fresh Evaluator. This is bounded dispatch work, not a team scheduler role. Follow the [periodic-check arrangement](../SKILL.md#arrange-periodic-team-checks), including its native timer limits.

At pause, stop or deadline, stop or transfer owned jobs and collectors, confirm their state and preserve results. Report an actual authorization or access blocker through the user entry point while unaffected work continues.
