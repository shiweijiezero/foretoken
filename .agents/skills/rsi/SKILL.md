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

While the Experimenter prepares the runtime and initial measurements, Advisors and Workers begin reading papers, official documentation, community implementations and local source to identify worthwhile questions. Work that can use these sources need not wait for available GPUs or a complete profile.

Before allocating resources, the lead checks the task's nodes, current users and available GPU windows, following the [resource coordination guidance](roles/common.md#coordinate-resources-and-environments). If a node cannot be used, the lead records the reason and addresses problems that can be resolved.

The lead establishes W&B naming before parallel experiments begin. An ongoing research topic uses one project, named with a `foretoken-` prefix and the model, hardware and objective, such as `foretoken-qwen35-metax-inference`. Each experiment uses a dated or named group, with run names distinguishing approaches, workloads and repetitions. Resumption keeps the original names, and experiment records retain the run links.

The lead establishes the user-facing `report.md` and its writer at task start, following the [report workflow](../../../rsi-playbooks/experiment-records.md#maintain-the-user-facing-report). The writer keeps it current as measurements and decisions change.

## Set up the team and assignments

The lead chooses which questions to pursue, gives each one time and resources, and keeps an owner following it. Ideas that need an implementation before their benefits can be judged may receive a trial; measurements determine whether to keep them. The Experimenter tests ready approaches within these arrangements. Members continue within the agreed scope without waiting for instructions at every step. See the [team guidance](roles/common.md) for assignments and handoffs.

Use the task's separate Advisor, Worker and Experimenter counts; each counts concurrent subagents in that role and excludes the lead. Respect user-specified counts, including zero. For unspecified counts, the lead chooses and records staffing from the work and available capacity. When a role has no dedicated member, the lead assigns its responsibilities to itself or another member; this does not create an additional member or establish independent review.

The harness's actual concurrency limit still applies, including any slot used by the lead. If it cannot accommodate the requested team, prioritize and stagger assignments within that limit, recording the requested and actual staffing and the reason for the difference. Count each member under one assigned role at a time.

[Advisors](roles/advisor.md) continually explore mechanisms, methods and new research questions, and help the team reassess its current direction. They investigate independently and may explore different questions or examine the same question from different angles, sharing findings as their research progresses.

[Workers](roles/worker.md) investigate specific questions and implement candidates, then improve them using experimental results. The implementation owner follows the end-to-end execution path to connect local changes with service behavior.

[Experimenters](roles/experimenter.md) manage the experiment queue and execution resources, request ready candidates from Workers, build and evaluate them, record results and release resources. One Experimenter can manage several non-conflicting experiments.

Assignments include absolute paths to the task, role instructions, code and evidence, together with the reference approach and relevant decisions. Members read the original sources themselves. Use stable names such as `worker-01` and `experimenter-01`, with the role prefixes `lead`, `advisor`, `worker` and `experimenter`. When the harness assigns the lead a messaging address, the team uses that address.

The lead follows the staffing above and reads experiment records and new results to track progress. Use observed durations, the number of candidates waiting and the remaining budget to adjust preparation work and concurrent experiments separately. Keep members who know a question working together; see [sharing findings and continuing work](roles/common.md#share-findings-and-keep-working).

## Isolate candidate workspaces and execution

Give each Worker a separate checkout, task branch and stable instance name, and reuse them across iterations. Keep the delivered version unchanged through build and evaluation, following the [candidate handoff](roles/common.md#prepare-candidates-while-evaluations-run).

Follow the repository's [deployment and iteration guidance](../../../AGENTS.md#deployment-and-iteration), reusing available compatible images and prepared environments first (which can substantially shorten deployment time). Parameter-only experiments reuse installed applications. For code changes, use an existing source platform or prepare it once, coordinating any shared-platform update with its administrator. Resolve model and dataset access during preparation.

Deploy Go, Rust and Python edits incrementally with `-e` pointing to the candidate's source directory. Follow the [iteration workflow](../../../rsi-playbooks/README.md#reuse-the-environment-across-iterations) and [parallel source deployment](../../../docs/deployment/source.md#run-candidates-in-parallel), sharing image references while keeping candidate code and writable build state separate. Deployment and evaluation use the same candidate name.

Candidates select the same shared [data directory](../../../docs/features/model-storage.md) for model downloads; runtime caches and results remain separate. Use `foretoken stop` to release idle serving resources while retaining reusable configuration and model references. Delete a candidate with `foretoken delete` when it is no longer needed; managed model versions can be collected after their last reference is released.

## Advance each iteration

At the start of an iteration, use research, code and earlier results to select the question and the next implementation or comparison that can answer it. The lead allocates time for both useful configuration experiments and mechanism changes that need further development. Workers continue owning their questions as measurements return. Existing [recipes](../../../benchmarks/docs/recipes.md), examples and CLI help guide execution, with resources and comparison conditions arranged before the run.

When preparation or measurement is blocked, the lead assigns an owner to resolve it while other members continue research, implementation or local validation that does not depend on that result. Candidates and validation methods can be prepared from available evidence before a full baseline or profile is available; measurements then determine whether to adopt them.

The Worker gives a runnable, reproducible version to the assigned Experimenter. Prefer a separate group of available GPUs for each experiment, arranging a shared schedule only when experiments need the same devices. The Experimenter deploys the change, confirms it took effect, records the measurements and sends results directly to the Worker and assigned reviewer. When GPUs are free, check which approaches are ready before asking for missing information. Fixed workload groups and repetitions can run through sweeps.

Before retaining or integrating a change, the lead reads the implementation and relevant call paths and reviews the independent assessment. The combined approach is still validated under the task's workload.

The lead continually adjusts directions and resources as research and experiments advance. When an Advisor flags stalled or misdirected work, the lead revisits the task objectives, current understanding and available results, explains whether to continue, change or stop the direction, and assigns the next action and owner. The responsible member fixes recurring execution problems and checks the affected workflow before the team relies on the fix.

When stopping a direction, state what the evidence ruled out and what remains unimplemented or unmeasured. An unavailable upstream option is an implementation constraint, not a performance result. Validation of the current best candidate can proceed while independent research continues within the remaining budget.

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

The lead checks retained changes and measurements, including sampling coverage and unresolved questions. The writer updates the existing report to reflect the pause or end state and objectives actually achieved, following the [report workflow](../../../rsi-playbooks/experiment-records.md#maintain-the-user-facing-report), and shares its path with the user.
