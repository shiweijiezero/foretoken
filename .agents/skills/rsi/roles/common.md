<!--
SPDX-License-Identifier: Apache-2.0
SPDX-FileCopyrightText: Copyright contributors to the Foretoken project
-->

# Working as an RSI team

English | [简体中文](common_zh.md)

A Worker turns an idea into a code or configuration change and hands that candidate to an Experimenter. The Experimenter evaluates it in an assigned environment and returns the results so the Worker can explain and improve the approach. Advisors continue exploring mechanisms and methods, while the lead uses the team's findings to adjust research priorities and resources.

Related work should stay with members who already understand it. This lets them build on earlier findings instead of reconstructing the same background for every iteration.

## Learn the framework first

Every role—the lead, Advisors, Workers and Experimenters—starts by learning how Foretoken is designed and used. Begin with the [project README](../../../../README.md), then use architecture guidance, relevant component documentation and maintained examples to understand how the control plane, data plane, inference engines and benchmark tools work together. This includes how services are deployed, requests are executed, and evaluations and results are produced.

With that shared understanding, each member studies the source, configuration and earlier experiments relevant to their question. They identify capabilities the framework already provides and the parts their work could affect. The lead supplies the source locations, and members read the originals and check them against the implementation. When versions or task scope change, refresh the affected parts of that understanding.

## Assign complete questions

The lead assigns a research question, the findings already available and the decision the work should inform. It provides the relevant code, references, configuration and experiment records, and names an owner who continues from investigation through implementation and interpretation of results. Method research starts alongside environment preparation; an unavailable GPU or incomplete baseline does not prevent reading papers, comparing implementations or investigating source.

Each assignment states what may change, the workloads and generation settings to preserve, and the time and resources available for investigation, implementation and measurement. Describe the next uncertainty to resolve, rather than requiring a gain before implementation begins. Every experiment has an executor, checkout, deployment environment and result location. Members continue within that scope without approval for each command; changes to the research priority, resource budget, shared environment or comparison conditions go to the lead.

Write the complete proposal in `<output-dir>/iterations/<name>/notes/`, where `<output-dir>` is the assigned experiment result directory. The first exchange includes a summary and the full document paths; relevant members read the proposal, code and evidence, discuss the approach and validation plan, and record decisions and open questions. See the [document collaboration guide](../../../../rsi-playbooks/experiment-records.md#share-documents-and-code) for the layout.

## Prepare candidates while evaluations run

When different members develop and evaluate an approach, record them as two tasks with a dependency between them. The Worker supplies a reproducible version, the question to test, reference results, workload, commands and resource needs, including device count and placement. The handoff identifies which components should rebuild or restart and which processes and caches should remain in use. The Experimenter uses these inputs to prepare and schedule the candidate.

The delivered checkout and execution environment stay unchanged while the candidate waits, builds or runs. Reuse the checkout after evaluation; create a separate development or evaluation copy only when further implementation needs to overlap those stages.

Use one set of task records for preparation, ready candidates, running experiments and completed results. Workers report candidate readiness; the Experimenter maintains the queue's readiness, execution owners and resource state. The lead sets research priorities and budgets, then adjusts preparation and evaluation capacity from those records. Reduce redundant queued comparisons when the backlog grows while continuing mechanism research within its allocated budget and interpretation of results.

Within the assigned priorities and budget, the Experimenter starts ready candidates that fit the available devices, host capacity and comparison conditions. Work that cannot yet run stays visible with its missing input or resource need; arrange an execution window for higher-priority multi-device experiments as resources become available. When capacity is idle, ask Workers for runnable versions and expected readiness, and report preparation bottlenecks to the lead.

For example, five Workers can prepare candidates for two GPUs. The Experimenter can run two independent one-GPU candidates or one two-GPU candidate while the remaining Workers continue preparing their versions.

Use existing task owners, dependencies and completion states. Assign each candidate to one executor in the shared queue before starting it; assigned or running work remains with that executor across resource pools. Experimenters may self-claim when task tools prevent duplicate claims. Otherwise, designate one Experimenter to assign queued work across the shared pools, with the other Experimenters executing their assignments. Without task tools, keep the same queue and ownership information in the iteration notes.

## Coordinate resources and environments

The lead defines execution-pool budgets and queue scheduling ownership. The Experimenter checks host GPU processes and Kubernetes workloads before using available devices within the task's authorization and shared-host arrangements, and records the actual allocation for each run. Coordinate changes to shared platforms and drivers with the administrator.

Budget serving, builds, model/data preparation and benchmark clients against current node capacity and existing Pod requests. Adjust [native build concurrency](../../../../docs/deployment/source.md#edit-an-inference-engine) when needed.

Preparation and evaluation can overlap when resources allow. For final comparisons, agree on a measurement window with the owners of workloads sharing the nodes, storage or network. Finish or stop this task's competing workloads, then measure the reference and retained approaches under the same conditions.

When a measurement finishes, continue ready work or stop unused serving processes and release their GPU allocation; analysis and review can continue from saved results. Retain reusable images, build caches and model references. Running jobs and collectors may instead be handed to an owner who accepts them. After an SSH disconnect, inspect the original job before restarting it.

## Return results as work finishes

Existing sweeps can run fixed workload groups and repetitions continuously. Task-owned remote sessions or an existing job manager preserve logs and exit status so jobs can be checked after a disconnect. When reading results, distinguish whether measurement finished, whether requests succeeded and whether the task's requirements were met.

After each group of measurements, the Experimenter sends results directly to the Worker and reviewer, updates the queue and resources, and continues already authorized work. The Worker interprets the measurements and advances the same research question. If priorities, budgets or comparison conditions need to change, the lead resolves that decision while unaffected work continues.

Experimenters maintain their iteration notes, and the lead assigns one writer to update the report. Summaries link to the original data so conclusions can be traced. Reviewed results remain useful while their inputs and conditions still apply. See the [experiment records guide](../../../../rsi-playbooks/experiment-records.md) for the record layout.

## Review claims independently

Before retaining an approach, the lead asks someone who did not implement or run it to check the results. The reviewer examines the actual code, runtime configuration and original data to confirm that the intended change ran. They then check precision, workload, output settings and resources, and consider whether failures, caches, warmup or competing work affected the comparison.

If a specific question remains unresolved, the reviewer explains what evidence or comparison would answer it. The Experimenter records that assessment separately from the developer's explanation. The lead then decides whether to investigate further, retain the approach or integrate it with other changes.

## Hand off and resume

When an assignment ends, the member reports the conclusion, result locations, open questions and resources still in use. Follow-up work can continue if it is already covered by the assignment; changes in scope or resources go to the lead. On resumption, check the records, remote jobs and team members before continuing with the original budget and completed results.

On a user pause, stop or deadline, the lead stops scheduling new experiments. Each owner stops or hands over their jobs and preserves the results. A GPU collector's deadline controls only that collector, so other jobs need their own stop action.

Count Workers, Experimenters and Advisors while they are actually doing assigned work, once per member. Exclude the lead and members who are waiting, blocked or finished. Mark intervals with insufficient evidence as unknown.
