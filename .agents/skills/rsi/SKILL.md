---
name: rsi
description: Execute, resume, pause, or finish an explicitly requested Foretoken RSI optimization task. Use only when the user asks to run RSI, not for ordinary coding or discussion of RSI designs.
---

<!--
SPDX-License-Identifier: Apache-2.0
SPDX-FileCopyrightText: Copyright contributors to the Foretoken project
-->

# Run an RSI task

English | [简体中文](SKILL_zh.md)

## Establish the task

Start experiments only when the user requests execution or resumption. Read the repository instructions, task, [team guidance](roles/common.md) and relevant records first. Distinguish requirements marked `[Fixed]` from starting implementation choices.

The root agent establishes the objective, original baseline, total resources, hard deadline and initial responsibilities. Resolve missing task settings at initialization. Resumption preserves that deadline and first inspects existing jobs and results. With the Agent Scheduler running, the root observes saved progress, checks resources and research direction, directs only concrete corrections and gives the user brief updates on effective gains, adopted configurations, blockers and next work. It does not become a per-stage dispatcher; use native follow-up only when scheduler health or an explicit dependency requires recovery. Members continue without waiting for the root to read, reply or approve; experiment records and exports remain with their producers.

Activate the task's [continuation binding](continuation.md#enable-the-task) and configure periodic checks when enabled. Use one W&B project for the research topic, named with a `foretoken-` prefix and the model, hardware and objective; retain its naming across resumptions. Begin research, implementation and environment preparation concurrently.

When the task must continue after the lead turn ends, start the task-bound [Agent Scheduler](continuation.md#run-the-independent-agent-scheduler). Hooks and queue operations persist responsibility but do not wake an idle native thread. Stop the scheduler at explicit pause, completion, expiry or deadline.

Each Experimenter writes its own iteration results and dispositions and refreshes the generated progress view. A designated overview owner maintains only report metadata, status, links and short context under the task-local report lock; the [report workflow](../../../rsi-playbooks/experiment-records.md#maintain-the-user-facing-report) keeps numerical aggregation in the plot command.

## Set up the team and assignments

Respect the task's separate Explorer, Worker and Experimenter concurrent counts; none includes the root. Explorer defaults to 4 if omitted or blank. Choose missing Worker and Experimenter counts at initialization. Evaluator count is the number of fresh agents per check, defaults to 1, and 0 disables checks. Pass the task's model and reasoning settings when creating members and record actual identities and roles. Respect harness concurrency and record any actual staffing limitation.

Give members complete research responsibilities and access to the task, role, current retained source and evidence. New Explorers start without inherited root history; continuing members reuse their research context. Members may explicitly cover a responsibility without a dedicated role. Do not use the root as an implicit replacement or manufacture work to fill a count; record real waits and capacity limits.

[Explorers](roles/explorer.md) use [PGE](../../../rsi-playbooks/guidance/meta-knowledge/03-exploration-and-decision.md) to choose and investigate hypotheses that improve the current best usable configuration. [Workers](roles/worker.md) implement and interpret candidates. [Experimenters](roles/experimenter.md) claim ready execution work, preserve results and release resources. Members submit work and claim it through [continuation](continuation.md), using topic and environment familiarity without fixed pairings or preassigning ready work to busy members.

[Evaluators](roles/evaluator.md) independently examine inference research and its workflow, using changed evidence and open issues to propose testable improvements through the same owner paths. Each check uses new agents without inherited conversation or saved context; supply only the task, role and working directory. They locate records themselves and deliver findings directly to the responsible owners.

## Arrange periodic team checks

At initialization, configure the existing periodic-check schedule using the task's interval and Evaluator count. Preserve its original anchor on resumption. An available registered member claims due dispatch work, creates the configured fresh Evaluators and completes that work with the actual saved assessment. This does not give that member team scheduling or result-transcription responsibilities.

The pool schedule is the task's only evaluation timetable. Live members consume due dispatch work; a blocking claim waits inside the tool without creating an independent background runtime. State an unavailable harness capability plainly. Do not start a second reminder-based evaluation schedule, stack checks or interrupt an experiment to make room.

Checks focus on changed evidence, important current issues and prior unresolved findings. Cover the system at initialization, a major change and completion. Findings go directly to the owner who can act within the task's authority; no routine root reflection or approval is required.

## Isolate candidate workspaces and execution

Reuse the installed platform, compatible runtime/compiler images, model files and retained caches. Each candidate has its own worktree, writable build state, deployment instance, runtime cache and device allocation. Multiple worktrees can use one prepared platform concurrently; queue only actual resource conflicts, coordinated by the affected Experimenters through existing Kubernetes resource mechanisms.

Apply source changes through [incremental deployment](../../../docs/deployment/source.md#deploy-a-model-service) with `-e` pointing to the candidate checkout. Compile model and native dependencies on the server in compatible images; lightweight local analysis is allowed. Confirm changed code is active before measurement. Keep the delivered candidate unchanged while it runs; development may continue in another worktree.

## Advance each iteration

All research, exploration and implementation default to the latest validated retained configuration applicable to the task and workload. Read it before starting a candidate, recording the actual parent and change in its notes. Use an old base only for an explicit reproduction or attribution question. If the retained parent changes during preparation, integrate the relevant changes and validate their affected behavior. Preserve the original source and results of a run already in progress.

A branch with useful results returns to the latest retained combination for measurement; isolated sibling gains do not establish a combined gain. Preserve the original baseline as the comparison reference. Research owners choose the next hypothesis, implementation and informative experiment, and decide local continuation, stopping and retention within the task's authority and total budget. Soft stage estimates may change; the hard deadline remains fixed.

Submit complete questions or informative stages, with code, evidence and comparison conditions in existing notes. Available qualified members claim work without fixed Worker–Experimenter pairings. Finish with original artifacts and deliver them directly; use native follow-up when a known idle consumer becomes ready. Continue independent work rather than waiting for a new root assignment.

Before adoption, obtain a fresh Evaluator assessment of the candidate's code, effective configuration and original evidence. Reuse applicable prior validation and choose new checks by important changed behavior. Do not automatically repeat controls or full matrices for each candidate, and obey task-specific measurement restrictions. A research owner adopts or revises the candidate using that assessment and the task's requirements; the combined deployment still needs evidence for the claimed scope.

Experimenters coordinate actual resource conflicts within the total budget. An owner resolves recurring execution defects in its path and verifies the affected workflow. Only changes beyond the authorized objective, resource budget or fixed requirements need new user authorization through the root entry point.

## Collect GPU usage

The Experimenter runs `scripts/gpu_usage.py` on each assigned GPU host. It requires Python 3.11+ and either `nvidia-smi` or `mx-smi`, and uses only the standard library. A host without a checkout needs only a copy of the script. Start collection through a task-owned persistent session or an existing job manager so it continues during analysis and after an SSH disconnect.

Before running the following command from the checkout root, set `GPU_RUN` to a new directory under the experiment results and `GPU_INDICES` to the assigned host GPU indices, separated by commas. Set `DEADLINE` to the task's absolute deadline with a UTC offset.

```bash
python3 -I .agents/skills/rsi/scripts/gpu_usage.py collect \
  --output "$GPU_RUN" --gpus "$GPU_INDICES" --deadline "$DEADLINE"
```

The collector prints its PID and output directory and appends samples to `samples.jsonl` every 10 seconds by default. Use `--interval` to change the interval. It detects the installed device tool; if both are installed, choose one with `--backend nvidia` or `--backend metax`. Omit `--gpus` only when the task owns all visible GPUs, and omit `--deadline` only for a task without a time budget. Resume collection in a new directory to preserve earlier samples.

To analyze a completed time window, set `WINDOW_START` and `WINDOW_END` to its ISO timestamps. The following command reads samples on the original host, shared storage or a local copy without interrupting collection.

```bash
python3 -I .agents/skills/rsi/scripts/gpu_usage.py summarize "$GPU_RUN" \
  --since "$WINDOW_START" --until "$WINDOW_END"
```

If the window spans several collection segments, pass all their directories, including resumed segments. The command calculates time-weighted utilization by node and GPU without double-counting overlapping or copied samples. Interpret the result alongside coverage, freshness and query errors; intervals without samples remain unknown.

When collection is no longer needed, run this on the collecting host:

```bash
python3 -I .agents/skills/rsi/scripts/gpu_usage.py stop "$GPU_RUN"
```

A stop marker, deadline, SIGINT or SIGTERM lets the collector exit after its current query, which is bounded to 10 seconds. The owner confirms the final `end` record and process exit before releasing it, and retains samples for the report. When stopping a process manually, check its identity to avoid signaling a reused PID or shared monitoring process.

## Pause or finish

On a user pause, stop, deadline or completion, stop claiming new work. Each owner stops or explicitly transfers its jobs and collectors, verifies exit and saves results. Retain reusable environments and caches as the task requires. Use [continuation](continuation.md#wait-pause-or-finish) to record task state; queued work is not evidence of a live agent or job.

Owners complete their existing experiment notes and dispositions so the [report workflow](../../../rsi-playbooks/experiment-records.md#maintain-the-user-facing-report) reflects actual achievements, retained changes and unfinished validation. Share the report path through the user entry point.
