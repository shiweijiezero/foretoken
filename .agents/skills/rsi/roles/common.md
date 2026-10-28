<!--
SPDX-License-Identifier: Apache-2.0
SPDX-FileCopyrightText: Copyright contributors to the Foretoken project
-->

# Working as an RSI team

English | [简体中文](common_zh.md)

A Worker turns an idea into a code or configuration change and hands that candidate to an Experimenter. The Experimenter evaluates it in an assigned environment and returns the results so the Worker can explain and improve the approach. Explorers continue exploring mechanisms and methods, while the lead uses the team's findings to adjust research priorities and resources.

## Learn the framework first

Research and execution members start with the [project README](../../../../README.md), architecture guidance, relevant component documentation and maintained examples. Learn how the control plane, data plane, inference engines and benchmark tools work together to deploy services, execute requests and produce results.

Then study the source, configuration and earlier experiments for the assigned question to identify reusable facilities and affected call paths. Use locations supplied by the lead and check originals against the implementation. Keep related work with members who understand it; they reuse established context and evidence, reading changed material or what a new judgment needs. Refresh affected knowledge when versions or scope change. [Evaluators](evaluator.md) start each fresh check from the task, role, prior findings and changed evidence. They consult unfamiliar framework paths as needed for the assessment.

## Assign complete questions

For each question, identify the next decision and the least costly evidence that can inform it. Reuse relevant research, prepared environments and comparable results before doing more work. Expand source reading, builds or measurements when the current evidence leaves a specific question unresolved; a fixed workload sweep is useful only while its remaining points can inform the decision.

The lead assigns a sustained research question with an owner, known findings, unresolved points and relevant code, references and experiment records. The owner follows it through investigation, implementation and analysis, identifying useful work that can continue while results are pending. Assign environment preparation separately so Explorers and Workers can proceed with source analysis, implementation and available checks.

Each assignment explains what may change, which workloads and generation settings must stay the same, and how much time and compute are available. Research tests uncertain benefits; a speedup need not be proven before implementation begins. Also name who will run the experiment, which checkout and environment to use, and where to save results. Members work independently within these agreements and ask the lead to coordinate changes to priorities, resource budgets, shared environments or comparison conditions.

Keep the complete proposal in `<output-dir>/iterations/<name>/notes/`, where `<output-dir>` is the experiment result directory. The [document collaboration guide](../../../../rsi-playbooks/experiment-records.md#share-documents-and-code) explains where to keep shared files and who writes them.

## Share findings and keep working

For example, an Explorer who finds a potentially faster method can send the findings and sources directly to the Worker studying that question. When the code is ready, the Worker asks the assigned Experimenter to test it. Reviewers also send specific issues to the person who can fix the code or run the missing comparison, without routing every exchange through the lead.

Use messages for short findings, questions and coordination. Put long explanations, full evidence or anything several people need to read in a shared file, then send a brief summary and its exact path. Give the background once and describe later changes without copying the whole document into messages. The [records guide](../../../../rsi-playbooks/experiment-records.md#share-documents-and-code) explains where files live and who writes them. Notify everyone when a change affects the whole team, and alert the executor immediately if an ongoing experiment will produce invalid results.

Evaluators read work and session records independently and send their assessment only to the lead. They identify discrepancies and decisions needing attention; the lead reflects on those findings and chooses any changes to direction, resources or assignments.

While waiting for a reply, continue work that does not need that answer. If progress really is blocked, explain what is missing and who can provide it. Routine updates do not need replies, and completion notifications remove the need to keep asking for status. Transfers of GPUs or running jobs still need someone to accept them. The lead follows the shared records and steps in when direction, scope or resources need to change.

Use session timelines to notice time spent repeating explanations, relaying messages, handing off work and relearning context. If communication displaces research or delays experiments, combine exchanges and use fewer, larger assignments with continuing owners. Judge the time spent and work delayed, not message count alone.

## Prepare candidates while evaluations run

The Worker prepares the version to test, and the Experimenter runs it; record that dependency in the tasks. With the code, supply the question to answer, reference results, commands, workload, GPU count and node placement. Explain which components need rebuilding or restarting and which processes and caches should remain in use, so the Experimenter can arrange the run directly.

The delivered checkout and execution environment stay unchanged while the candidate waits, builds or runs. Reuse the checkout after evaluation; create a separate development or evaluation copy only when further implementation needs to overlap those stages.

Give each candidate one Experimenter, keeping the same member with a research direction where possible. That owner records candidates being prepared, ready or running, plus the group's job, service, collector and resource state in existing task tools or iteration notes, linking completed results. If a shared resource record needs one writer, assign an Experimenter when one is staffed; other executors send it their changes. Transfer record ownership with the work. The lead uses these records for priorities, budgets and cross-group decisions.

For example, five Workers can prepare approaches even when only two GPUs are available. The Experimenter runs two one-GPU experiments or one two-GPU experiment according to priority and available resources, while the other Workers continue. Record what is missing for work that cannot run yet. Arrange a measurement window for higher-priority multi-GPU work as devices become free. When GPUs are idle, check the records before asking Workers for missing versions or delivery estimates; involve the lead when the research schedule needs to change.

When experiments share GPUs or combine devices from several groups, one Experimenter arranges their order while other groups continue. Agree on the handoff before changing executors. If candidates accumulate, drop redundant comparisons while Workers continue research and analysis.

## Coordinate resources and environments

With six suitable GPUs free, three two-GPU experiments can each use their own pair instead of taking turns. Prefer this arrangement within the task's budget. One Experimenter manages each group and may manage several groups. Across consecutive runs, keep the same devices and executor where possible so each group can deploy, measure and clean up independently. Queue experiments when there are not enough GPUs.

Before using GPUs, the Experimenter checks host processes and Kubernetes workloads, confirms that the task may use the devices and that no one else is using them, and records the devices used. Release GPUs when no experiment is ready to follow, and check availability again before reuse; do not reserve them for work that is still being prepared. Coordinate shared-platform and driver changes with the administrator.

Budget serving, builds, model/data preparation and benchmark clients against current node capacity and existing Pod requests. Adjust [native build concurrency](../../../../docs/deployment/source.md#edit-an-inference-engine) when needed.

Preparation and evaluation can overlap when resources allow. For final comparisons, agree on a measurement window with the owners of workloads sharing the nodes, storage or network. Finish or stop this task's competing workloads, then measure the reference and retained approaches under the same conditions.

When a measurement finishes, continue ready work or stop unused serving processes and release their GPU allocation; analysis and review can continue from saved results. Retain reusable images, build caches and model references. Running jobs and collectors may instead be handed to an owner who accepts them. After an SSH disconnect, inspect the original job before restarting it.

## Return results as work finishes

Existing sweeps can run fixed workload groups and repetitions continuously. Task-owned remote sessions or an existing job manager preserve logs and exit status so jobs can be checked after a disconnect. When reading results, distinguish whether measurement finished, whether requests succeeded and whether the task's requirements were met.

The Experimenter owns a run through result delivery and resource release. The lead names the Worker and report writer, and an independent reviewer when the comparison needs one. As each run finishes, the Experimenter reads its outcome, makes the original results accessible at the agreed location and sends the same finding and links directly to these recipients. This includes diagnostic controls saved outside a benchmark's `runs/` directory. See [complete the experiment notes](../../../../rsi-playbooks/experiment-records.md#complete-the-experiment-notes) for delivery contents and remote recovery.

Deliver each completed measurement point without waiting for the full workload group. The Worker analyzes available evidence while the Experimenter continues remaining measurements; pending points stay explicit. Any assigned reviewer checks the claim, and the writer updates the report. These stages proceed without a new lead assignment after every point. If a recipient is idle, use the harness's native follow-up, or ask the lead when only it can resume the member. A message or queued step alone does not resume an idle member.

The Experimenter or an explicitly accepting successor remains responsible for undelivered results. Record missing access and the person taking the next action in the existing iteration notes. The lead resolves changes to priorities, budgets, comparison conditions and retention while unaffected work continues. Reviewed results remain useful while their inputs and conditions still apply.

## Review claims independently

Choose validation from the change's effect on model outputs. If outputs are unaffected or existing quality evidence still applies, record that basis and reuse the conclusion. When a change may affect outputs and the evidence is insufficient, validate the affected behavior within the task's evaluation budget and frequency; run quality scoring only when needed.

Before retaining an approach, the lead asks someone who did not implement or run it to check the results. The reviewer examines the actual code, runtime configuration and original data to confirm that the intended change ran. They then check precision, workload, output settings and resources, and consider whether failures, caches, warmup or competing work affected the comparison.

If a specific question remains unresolved, the reviewer explains what evidence or comparison would answer it. The Experimenter records that assessment separately from the developer's explanation. The lead then decides whether to investigate further, retain the approach or integrate it with other changes.

## Hand off and resume

A stage delivery reports the conclusion, evidence and next useful step while the owner continues work within the assignment. If progress needs a result or decision, name that dependency and the condition for resuming. When the research question is answered, report remaining questions and resources still in use.

On a native completion or idle notification, the lead checks current role staffing against the task. Resume the familiar member with a concrete next assignment, assign another owner, or record the real dependency or reason for ending that responsibility. If an Explorer moves into implementation or review, count the actual role and keep exploration assigned. On task resumption, check records, jobs and members, retaining the original budget and completed results.

On a user pause, stop or deadline, the lead stops scheduling new experiments. Each owner stops or hands over their jobs and preserves the results. A GPU collector's deadline controls only that collector, so other jobs need their own stop action.

Count Workers, Experimenters, Explorers and Evaluators while they are actually doing assigned work, once per member. Evaluators count only during a check, not between rounds. Exclude the lead and members who are waiting, blocked or finished. Mark intervals with insufficient evidence as unknown.
