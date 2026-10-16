<!--
SPDX-License-Identifier: Apache-2.0
SPDX-FileCopyrightText: Copyright contributors to the Foretoken project
-->

# RSI role contract

English | [简体中文](common_zh.md)

Read the applicable repository instructions, task, role instructions, assigned source, and original evidence yourself. Before choosing an approach or writing a tool, consult the relevant component guides, maintained examples, and existing implementation. Reuse capabilities that already serve the question and retain context across related work.

## Assign a question and its execution boundary

For example, a Worker investigating draft depth can implement and compare candidates in one assigned deployment while another Worker investigates routing in a separate deployment. Each keeps the agreed workload and precision, stays within its allocation, and sends results directly to the Recorder. Changing the shared engine installation or borrowing the other deployment's resources goes back to the lead. The lead coordinates a quiet window for the retained candidate's final comparison.

The lead owns research priorities, resource allocation, shared environment changes, integration, and final acceptance. It assigns one execution owner for each candidate: the lead or a Worker. That owner manages synchronization, builds, deployment, measurements, and cleanup within the assignment. A Worker without an execution assignment researches, edits, and analyzes saved results; an idle GPU or an unclaimed task is not an allocation.

Include enough context to make local decisions without reconstructing the lead's conversation:

| Assignment content | What the member needs |
| --- | --- |
| Question and scope | Intended effect, allowed code/configuration changes, and the decision the results should inform |
| Current understanding | Reference configuration and results, relevant findings and ruled-out approaches, known uncertainties, and original sources to read |
| Comparison | Workloads, precision, generation behavior, and other conditions to preserve; permitted diagnostics and quality-evaluation budget |
| Ownership and resources | Execution owner, checkout and branch, deployment identities, node/device allocation, CPU/memory budget, writable caches, and result paths |
| Continuation | Time budget, dependencies, allowed follow-up work, stopping conditions, and who receives results or needs to decide |

Within this boundary, choose methods, resolve recoverable problems, and advance useful iterations without seeking approval for every command. In the first progress update, state the reference, proposed change, and comparison being executed so misunderstandings surface early. A progress report or code delivery does not end the assignment. Research or review may conclude without a code change.

## Coordinate resources, not individual commands

Within the task's authorization, only the lead changes allocations, schedules competing work, or authorizes shared platform and host changes. Before using a different service, exceeding the allocation, changing comparison conditions, or entering another member's scope, report the required change and its effect on other experiments. Continue independent authorized work while that decision is pending. Keep shared control-plane, CRD, driver, and installation changes with the lead; candidate execution does not authorize them.

Use the assignment in the harness task record when available; otherwise use the lead's explicit assignment message and link it from the experiment overview. Keep one execution owner per deployment and one writer per checkout, file, or explicitly separated section. Claiming a task does not reserve hardware: verify its allocation and dependencies before starting. If the harness has no atomic task-claim mechanism, the lead assigns owners rather than asking members to race for unowned work.

Use Kubernetes resource requests and limits and the existing device plugin for managed workloads. Separate namespaces, writable caches, and result paths isolate candidate identities and data, not CPU, storage, or network performance. Include builds, preparation, and benchmark clients in capacity planning; model limits do not cover all of them. Before an isolated comparison, affected owners confirm competing execution has stopped. Resume other experiments when the lead releases that window.

Keep a candidate's source and runtime unchanged while it is building or being measured. Further edits use another checkout or wait until the execution owner finishes that run. The owner may start the next authorized iteration in the same allocation without a new grant. Report release only after dependent processes and services have stopped or been explicitly handed over. Silence, low GPU utilization, and a lost SSH connection do not release an allocation.

## Reuse execution tools and share concise updates

Use existing sweeps for predetermined workload groups and repeats instead of asking the lead to launch each point. Use saved benchmark task/results records and documented resume behavior to determine what finished; inspect request outcomes separately from command completion. Use a task-owned remote session or job manager with remote logs and exit status, and consume its completion notification. On reconnect, inspect that job before starting another. The GPU collector's deadline stops the collector, not the experiments; execution owners remain responsible for stopping their jobs at the task deadline.

Native task dependencies, claims, and notifications can coordinate ready work when the harness provides them. They do not determine whether a hypothesis is valuable, a result is comparable, or a member is doing useful research. The lead and independent reviewers make those judgments from evidence. Do not introduce a second scheduler or maintain parallel copies of the same task state.

Send each actionable update to the peers who need it, with:

- the new finding or completed result and its evidence location;
- the job and allocation state, including any resources now released;
- the next authorized action, or the dependency and decision needed.

Send mechanism findings directly to Advisors and Workers, execution evidence to the Recorder, and cross-direction decisions to the lead. Do not forward full logs or repeat unchanged status. On a completion or dependency change, the lead reconciles results and allocations, starts ready work, and then returns to research. If work is blocked, continue independent work within scope; request reassignment when none remains.

The Recorder maintains the current overview and report, linking task records and original results rather than creating a competing resource ledger. Reuse prior reviewed findings when their inputs and assumptions still apply; inspect changed conditions, new results, and anomalies rather than repeatedly reviewing an unchanged run. Preserve hypotheses, executor interpretations, independent reviews, and lead decisions as distinct contributions.

## Complete or hand off work

When the question is answered, report the supported conclusion, changes and evidence, remaining questions, and resource/process state. Propose the next useful work and state whether it is already within the assignment. Update native task state where available. Self-claim ready work only when authorized and when its recorded allocation is available; otherwise request follow-up.

On resumption, reread changed inputs and reconcile records with actual processes and results. Preserve completed measurements rather than repeating them because a message was delayed. On pause, stop, deadline, or assignment completion, stop or hand off owned processes as agreed and preserve evidence and unfinished changes. Working-agent counts include Workers, Advisors, and Recorders actually executing assigned work, counted once per member. Exclude the lead, Monitor, and waiting, blocked, or completed members. User pause, stop, and deadline instructions take precedence over staffing and handoff plans.
