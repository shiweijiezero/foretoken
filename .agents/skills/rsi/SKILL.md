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

Before starting, every role [learns how Foretoken works](roles/common.md#learn-the-framework-first). Members also read the repository instructions, the user's task and relevant experiment records, using the [research library](../../../rsi-playbooks/guidance/README.md) for the current question. Relative links in this document resolve from the skill directory.

The team starts experiments only when the user requests execution or resumption. A new task records its start and deadline; a resumed task first checks existing jobs and results and keeps the original time budget. A supplied configuration is a starting point that can evolve with evidence. If none is supplied, the team records an initial approach for comparison. The team builds on existing findings and continues until the task's end conditions are met.

Before allocating resources, the lead checks every node in the task and its current users. Capacity planning includes model services, builds, preparation jobs and benchmark clients. If a node cannot be used, the lead records the reason and addresses problems that can be resolved.

The lead establishes W&B naming before parallel experiments begin. An ongoing research topic uses one project, named with a `foretoken-` prefix and the model, hardware and objective, such as `foretoken-qwen35-metax-inference`. Each experiment uses a dated or named group, with run names distinguishing approaches, workloads and repetitions. Resumption keeps the original names, and experiment records retain the run links.

## Set up the team and assignments

The lead develops research directions, assigns questions and resources, and combines results to decide which approaches to retain. Members work on complete questions and continue within the agreed scope without waiting for individual commands. The [team guidance](roles/common.md) explains assignments and handoffs.

[Advisors](roles/advisor.md) continually explore mechanisms, methods and new research questions, and help the team reassess its current direction. The lead assigns at least two Advisors to investigate independently. They may explore different questions or examine the same question from different angles, sharing findings as their research progresses.

[Workers](roles/worker.md) investigate specific questions and implement candidates, then improve them using experimental results. At least one Worker follows the end-to-end execution path to connect local changes with service behavior.

[Experimenters](roles/experimenter.md) build and evaluate prepared candidates while observing resources, recording results and cleaning up experiments. The lead chooses their number from evaluation capacity; small tasks can combine development and execution in one member.

Assignments include absolute paths to the task, role instructions, code and evidence, together with the reference approach and relevant decisions. Members read the original sources themselves. Use stable names such as `worker-01` and `experimenter-01`, with the role prefixes `lead`, `advisor`, `worker` and `experimenter`. When the harness assigns the lead a messaging address, the team uses that address.

During execution, the lead maintains the task's required number of working members and tracks candidates being prepared, waiting for evaluation and executing. Preparation and evaluation concurrency can change independently according to observed durations, backlog and remaining time. During an isolated final measurement, other members continue research and analysis that does not compete for the affected resources.

## Isolate candidate workspaces and execution

The team edits code on task branches and gives concurrent writers separate checkouts. Independent clones can provide separate execution directories; switching branches in one shared directory does not isolate simultaneous edits. The lead records source versions, responsible members and resource assignments.

The shared CLI and platform are prepared using the [source deployment guide](../../../docs/custom-deployment.md). The lead coordinates changes to CRDs, drivers and shared platform installations, while each candidate uses its own deployment, writable cache and result directory. Read-only model data can be reused where suitable.

The executor runs from the candidate checkout using its Kustomize configuration and namespace. When running from another directory, pass `-e` with the checkout and an absolute deployment path. Candidate engine changes use `--engine-source`; subsequent runs reuse the recorded selection and incremental builds.

Delivered code and the runtime environment stay unchanged during build and measurement. Task-owned remote sessions or an existing job manager preserve logs and exit status, and the executor checks the original job after a disconnect before deciding to restart it. A checkout and its resources can be removed after required changes and results are saved and their users have finished. Preserve unfinished work on pause and inspect the Git index before committing.

## Advance each iteration

At the start of an iteration, the lead, Advisors and Workers use code, research and earlier results to choose a useful question and discuss a comparison that can answer it. Existing [recipes](../../../benchmarks/docs/recipes.md), examples and CLI help guide execution, with resources and comparison conditions arranged before the run.

The Worker implements and explains the approach, then the Experimenter builds and measures it, confirming that the intended change ran. Fixed workload groups and repetitions can run through sweeps. Results return directly to developers and reviewers so analysis, preparation and recording can continue while other evaluations run.

Before retaining or integrating a change, the lead reads the implementation and relevant call paths and reviews the independent assessment. The combined approach is still validated under the task's workload, using a quiet measurement window when required.

New findings can change research priorities and resource needs, so the lead adjusts subsequent work accordingly. Recurring execution problems are addressed by the responsible member, and the affected workflow is checked before the team relies on the fix.

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

On a user pause, stop or deadline, the lead stops scheduling new experiments and notifies the team. Each owner stops their processes and confirms exit. Unfinished work is preserved on pause; at task completion, required results are saved before unused resources are removed.

The team completes the deliverables using the [experiment records guide](../../../rsi-playbooks/experiment-records.md). The lead checks retained changes and measurements, including sampling coverage and unresolved questions, and explains which objectives were achieved.
