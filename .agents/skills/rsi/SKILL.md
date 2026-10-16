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

Read the repository instructions, the user's task and relevant experiment records. Use the [research library](../../../rsi-playbooks/guidance/README.md) for the current question. Resolve these links from the skill directory.

Start experiments only when the user requests execution or resumption. Record a new task's start and deadline; on resumption, preserve the original budget and check existing jobs first. Use the supplied configuration as a starting point that can evolve. If none is supplied, record an initial approach for comparison. Carry forward useful findings and unresolved questions, and continue exploring until the task's end conditions are met.

Check all assigned nodes and their current users before allocating resources. Include preparation and benchmark clients as well as model services. Record what prevents an allocated node from being used and address recoverable problems.

For W&B, use one project per ongoing topic, with a `foretoken-` prefix and the model, hardware and goal, such as `foretoken-qwen35-metax-inference`. Use one dated or named group for the experiment and descriptive run names for candidates, workloads and repetitions. Keep these names on resume and retain links in the experiment records.

## Set up the team and assignments

The lead develops research directions, assigns questions and resources, and decides what to retain. Members work on complete questions rather than waiting for individual commands. Follow the [team guidance](roles/common.md) for assignments, resource handoffs and independent review.

| Role | Assignment |
| --- | --- |
| Lead | Coordinate shared changes, combine findings, review retained code, and own final validation and delivery |
| [Advisor](roles/advisor.md) | Explore methods and mechanisms, uncover new questions and challenge the current direction; assign at least two Advisors to complementary questions |
| [Worker](roles/worker.md) | Investigate and implement approaches, then revise them using results; at least one member follows the end-to-end execution path |
| [Experimenter](roles/experimenter.md) | Build and evaluate prepared candidates, observe resources, record results and clean up; choose staffing from evaluation capacity |

Provide absolute paths to the task, role instructions, code and evidence, along with the reference approach and decisions that matter. Members read these sources themselves. Use stable names such as `worker-01` and `experimenter-01`; the role prefixes are `lead`, `advisor`, `worker` and `experimenter`. Keep the lead's actual messaging address when the harness assigns it.

The lead maintains the task's required number of working members using actual progress and the counting rules in the team guidance. Track candidates being prepared, waiting and executing, and adjust preparation and evaluation concurrency from observed durations and backlog. When results arrive, arrange ready work promptly. During isolated final measurements, other members continue work that does not compete for the affected resources.

## Isolate candidate workspaces and execution

Work on task branches, with separate checkouts for concurrent writers. Use separate clones when independent execution directories are needed; switching branches in one shared directory does not isolate edits. The lead records the source revision, paths, writers and resource assignments.

Prepare the shared CLI and platform once using the [source deployment guide](../../../docs/custom-deployment.md). Shared controller, CRD, driver and installation changes stay with the lead. Give each candidate its own deployment identity, writable cache and result directory; reuse read-only model data where suitable.

The assigned executor runs from the candidate checkout and its Kustomize namespace. From another directory, pass `-e` with the checkout and use an absolute deployment path. Use `--engine-source` for a candidate engine checkout and reuse the recorded selection and incremental builds in later runs.

Keep the delivered code and runtime unchanged while building or measuring. Use task-owned remote sessions or an existing job manager to preserve logs and exit status. Check the original job after a disconnect. Release or remove a checkout and its resources only after required changes and results are saved and their users have finished. Preserve unfinished work on pause, and inspect the Git index before committing.

## Advance each iteration

1. Use code, research and earlier results to choose a useful question. Agree on comparisons and resources before execution, using existing [recipes](../../../benchmarks/docs/recipes.md), examples and CLI help.
2. The Worker implements and explains the approach. The Experimenter builds and measures it, checking that the intended change ran. Use sweeps for fixed workload groups and repetitions.
3. Return results directly to the developer and reviewers. Continue preparation and analysis while other evaluations run, and keep the experiment records current.
4. Before retaining or integrating a change, the lead reviews its implementation, relevant call paths and the independent assessment. Validate the combined approach under the task's workload, arranging quiet measurement windows when needed.
5. Revisit directions and resource use as findings arrive. Resolve recurring execution problems at their existing owner and verify the affected workflow before relying on the fix.

## Collect GPU usage

Run `scripts/gpu_usage.py` from this skill on each GPU host using Python 3.11+ and the installed `nvidia-smi` or `mx-smi`. It uses only the standard library; a host without a checkout needs only a copy of this script. Use the existing remote job manager or a task-owned persistent shell session so collection survives the collection owner's analysis and SSH disconnection.

From the checkout root on each host, set `GPU_RUN` to a new directory under the experiment's results, `GPU_INDICES` to that node's assigned host indices (comma-separated), and `DEADLINE` to the task's absolute ISO timestamp with UTC offset. Start once:

```bash
python3 -I .agents/skills/rsi/scripts/gpu_usage.py collect \
  --output "$GPU_RUN" --gpus "$GPU_INDICES" --deadline "$DEADLINE"
```

The collector prints its PID and output directory, then appends to `samples.jsonl` every 10 seconds. Use `--interval` to change the sampling interval. Backend detection selects the installed device tool; use `--backend nvidia` or `--backend metax` when both exist. Omit `--gpus` only when the task owns all visible GPUs, and omit `--deadline` only for a task without a time budget. Existing sample files are never overwritten; each resumed collection uses a new directory.

For periodic analysis, set `WINDOW_START` and `WINDOW_END` to the next completed window's ISO timestamps. Read the files on their host or through existing shared storage; the summary command may also read copies on the collection owner's machine. It does not start or interrupt collection:

```bash
python3 -I .agents/skills/rsi/scripts/gpu_usage.py summarize "$GPU_RUN" \
  --since "$WINDOW_START" --until "$WINDOW_END"
```

Pass all collection directories for the requested window, including resumed segments. The command produces time-weighted usage per node and GPU without double-counting overlaps or copied records. Read utilization together with coverage, freshness, and query errors; missing data is unknown, not idle time.

Request a stop on the collecting host:

```bash
python3 -I .agents/skills/rsi/scripts/gpu_usage.py stop "$GPU_RUN"
```

The stop marker, deadline, SIGINT, or SIGTERM ends collection after any in-flight query (bounded to 10 seconds). Confirm the final `end` record and process exit before releasing the collector. Keep its samples for the experiment report; on resumption start a new run directory. Do not signal a reused PID or stop shared monitoring.

## Pause or finish

On a user pause, stop or deadline, stop scheduling experiments and notify the team. Each owner stops their processes and confirms exit. Preserve unfinished work on pause; at completion, save required results and remove resources that are no longer used.

Use the [experiment records guide](../../../rsi-playbooks/experiment-records.md) to complete the task's deliverables. The lead reviews the retained changes and measured results, including sampling coverage and unresolved problems, and reports what was achieved.
