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

Choose collaboration and continuation tools that fit the task and the current harness. Use the same role responsibilities across environments; check which goal, delegation, messaging, task, and monitoring capabilities are available. Keep coordination work proportional to the experiment and make resource ownership, progress, and stopping conditions visible to the team.

## Establish the task

Read the repository instructions, the task supplied by the user, [shared workflow](../../../rsi-playbooks/README.md), [execution rules](../../../rsi-playbooks/tasks/README.md), and relevant prior experiment records. Members select references and prior findings from [shared guidance](../../../rsi-playbooks/guidance/README.md) for their current questions. Resolve these links from this skill directory. Keep task parameters in the task rather than copying defaults into this skill.

If the task supplies a starting configuration, check it against the task and current environment and treat it as an evolving starting point, not a fixed strategy. Otherwise, explore autonomously and record an initial configuration for comparison. In either case, carry forward applicable historical results, ruled-out directions, and unresolved questions. When current candidates are exhausted, revisit the direction and research new methods rather than automatically ending the task. Choose methods from the goal, evidence, and remaining budget; completion follows the task's end conditions.

Determine the requested action before starting anything. A pause, stop, or design discussion does not authorize starting or resuming experiments. For a new execution, record the task start and any deadline in its experiment notes. On resumption, read the existing timing and resource ownership first; preserve the agreed budget rather than resetting it.

Include every node listed in the task in the experiment plan. Check availability and existing owners, allocate independent candidates by their actual requirements, and record actionable blockers for unused nodes. Distinguish the whole resource pool from a per-service limit.

When using W&B, agree on readable tracking names before parallel runs. Use `--wandb-project` for an ongoing optimization topic, with a `foretoken-` prefix and a name identifying the model, hardware, and objective, such as `foretoken-qwen35-metax-inference`. Use one `--wandb-group` per RSI experiment, identified by its date or experiment name, to collect all iterations and parallel candidates. Use `--wandb-run-name` to distinguish the iteration, workload, concurrency, and repeat. The lead uses the same project and group for all candidates in the experiment. Resume with the same names; a new experiment for the same topic uses a new group in the existing project. Record names and run links in the experiment overview and iteration notes so Workers and Experimenters can find and compare results.

## Set up the team and assignments

The lead develops and revises directions, allocates resources, synthesizes findings, and selects retained changes. It owns shared environment preparation, cross-candidate coordination, integrated validation, and final delivery. Each evaluation has one execution owner, normally an Experimenter; the lead or a Worker can combine that role when useful. Use the [role contract](roles/common.md) to define that owner's context, resources, comparison conditions, and permitted continuation before execution.

Give Workers complete research questions rather than individual commands. Separate candidate preparation from evaluation when they benefit from independent concurrency: Workers prepare versions and interpret results, while Experimenters consume ready versions within their allocations. Keep execution with the lead when shared dependencies or isolation remain unresolved. The lead still studies the system, connects findings across directions, and develops new questions with Advisors rather than only processing a backlog.

Pass absolute paths to the role contract, role file, task, repository instructions, relevant source, and original evidence. Members read these themselves; they do not inherit the lead's conversation. Include the current reference, known uncertainties, and prior decisions that affect the assignment.

| Role instructions | Responsibility and scaling | Record ownership |
| --- | --- | --- |
| Lead | Sets directions, assignments and allocations; owns shared changes, retention decisions, integrated validation and delivery. May execute candidates or delegate their execution within explicit boundaries. | Assignments, allocations and decisions; the designated writer maintains the overview. |
| [Advisor](roles/advisor.md) | At least two members pursue complementary research, mechanisms, alternatives, and reflection. The lead chooses staffing from useful perspectives and the budget. | Findings and recommendations supplied directly to Workers, the lead and Experimenters. |
| [Worker](roles/worker.md) | Researches, implements and analyzes an assigned question; owns candidate execution when assigned. At least one follows the end-to-end path. Worker count need not equal experiment count. | Code, executed commands and results when responsible, analysis, and resource handoff. |
| [Experimenter](roles/experimenter.md) | Executes ready candidates, observes allocated resources and owns factual records. Scale with evaluation capacity, not Worker count. Independent review is assigned to an uninvolved member. | Commands, runtime identity, results, collector records and iteration notes; one designated writer owns the overview and report. |

Name members `<role>-<number-or-code>` in lowercase, using `lead`, `advisor`, `worker`, or `experimenter`, for example `worker-01` or `worker-alpha`. Names identify members, not directions; reuse them for related assignments. If the harness fixes the lead's messaging address, retain that address.

Maintain the task's required useful parallelism throughout execution. Count Workers, Experimenters and Advisors once per member while actually doing assigned work; exclude the lead and waiting, blocked or completed members. Combining roles does not add members. Reconcile native task state and actual progress on completion and dependency changes. Mark unsupported intervals unknown rather than deriving work from a resident Pod or nominally running agent. If useful work cannot meet the requested count, report its concrete readiness or resource blocker.

Keep preparation, waiting-for-evaluation and executing candidate counts visible through existing task records and the experiment overview. Adjust preparation concurrency and evaluation concurrency separately using observed durations and backlog, following the role contract. After a result arrives, reconcile the allocation and start the next authorized work before returning to independent research. Use atomic self-claiming only where the harness supplies it and the lead has already assigned the necessary resources. Otherwise the lead assigns ownership. Final isolated comparisons take precedence over competing execution on the affected resources; continue research and analysis elsewhere within the task's limits.

On resumption, read the original budget, current overview, allocations, and job identities. Check which members and processes actually exist before reassigning work. Recreate missing members where needed, without restarting completed measurements or resetting the budget.

## Isolate candidate workspaces and execution

Use a task branch rather than editing `main`. Parallel writers need separate checkouts and branches: switching branches in one shared directory does not isolate concurrent edits. Use independent clones when separate execution directories are needed; worktree-isolated agent sessions are not required. The lead coordinates the base revision, absolute paths, writers, execution owners, resource allocations, and result locations.

Prepare the shared CLI and source platform once using the [source deployment guide](../../../docs/custom-deployment.md). Only the lead coordinates shared controller, CRD, installation, and host changes. Each candidate uses its own deployment identity, writable runtime cache and result directory; share read-only model data where appropriate.

The assigned execution owner synchronizes and runs the candidate from its checkout, using its Kustomize namespace. When running elsewhere, pass `-e` with the candidate checkout and an absolute deployment path. Bind the candidate's engine checkout with `--engine-source` when modifying engine code. Reuse recorded source selections and existing incremental builds rather than reinstalling the platform per candidate.

Keep source and runtime unchanged during build and measurement. Further development uses another checkout or waits for the current run to finish. Save remote logs, job identity and the actual command's exit status through a task-owned persistent session or existing job manager; after disconnection, inspect that job before starting another. Resource ownership continues until its executor confirms release or handoff, as described in the role contract.

Remove a candidate checkout or runtime only after its changes and evidence are retained or explicitly discarded and its processes and services have no remaining consumer. Preserve unfinished work on pause and deliberately reuse or reassign it on resumption. Coordinate Git operations and inspect the actual index before committing.

## Advance each iteration

1. The lead, Advisors and Workers choose hypotheses from code, references and existing results. Consult the deployment guide, [benchmark recipes](../../../benchmarks/docs/recipes.md), maintained examples and relevant CLI help. Decide the comparison conditions and allocate execution before committing resources.
2. The Worker develops and explains a candidate and its comparison. The assigned owner builds, deploys and diagnoses it within scope, checking that the intended execution path ran. Use existing sweeps for predetermined workload groups and repeats; independent owners can run non-conflicting candidates concurrently.
3. The Experimenter records results, configuration and resource state and sends them directly to the Worker, lead and reviewer. Workers interpret effects and prepare further candidates. An Advisor or another member uninvolved in that candidate's implementation and execution independently reviews claims; the assigned writer incorporates that review without reinterpreting it as an execution fact.
4. Before retaining or integrating a change, the lead personally reads its implementation and relevant call paths, reviews architecture and activation conditions, and considers the independent evidence. It assigns integration work and owns validation of the combined result, arranging quiet measurement windows where required.
5. Reconsider directions, staffing and allocations as findings arrive. Fix recurring execution obstacles at their existing owner: the lead coordinates shared environment corrections, while Workers implement assigned code fixes and the execution owner validates the affected workflow. Keep analysis, recording and useful independent work moving within the remaining budget.

Follow the role contract for updates and handoffs. Existing runners, resource controls, task records and notifications handle execution facts; research priorities and acceptance remain evidence-based decisions, not an assumed automatic RSI scheduler.

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

A user pause or stop and the task deadline take precedence over queued messages, new-task requests, and staffing objectives. Stop assigning experiments, notify members, and have resource owners stop their processes and confirm exit; use native cancellation when needed. Preserve resumable work on pause. At completion, assign cleanup of resources with no remaining consumer after preserving required evidence.

Have the assigned writers complete delivery at the task's paths following the [experiment records guide](../../../rsi-playbooks/experiment-records.md). The lead accepts the delivery and summarizes retained changes, measured results, GPU sampling coverage, and unresolved blockers. Do not present role instructions or historical validation as evidence that the current team, experiments, or supervision actually ran.
