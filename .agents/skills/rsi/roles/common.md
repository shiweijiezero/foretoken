<!--
SPDX-License-Identifier: Apache-2.0
SPDX-FileCopyrightText: Copyright contributors to the Foretoken project
-->

# Working as an RSI team

English | [简体中文](common_zh.md)

A Worker turns an idea into a code or configuration change and hands that candidate to an Experimenter. The Experimenter evaluates it in an assigned environment and returns the results so the Worker can explain and improve the approach. Explorers continue exploring mechanisms and methods, while the lead uses the team's findings to adjust research priorities and resources.

Related research and execution work should stay with members who already understand it. This lets them build on earlier findings instead of reconstructing the same background for every iteration. [Observers](observer.md) are the exception: the lead creates new agents for each team check.

## Learn the framework first

Every role, including the Observer, starts by learning how Foretoken is designed and used. Begin with the [project README](../../../../README.md), then use architecture guidance, relevant component documentation and maintained examples to understand how the control plane, data plane, inference engines and benchmark tools work together. This includes how services are deployed, requests are executed, and evaluations and results are produced.

With that shared understanding, each member studies the source, configuration and earlier experiments relevant to their question. They identify capabilities the framework already provides and the parts their work could affect. The lead supplies the source locations, and members read the originals and check them against the implementation. When versions or task scope change, refresh the affected parts of that understanding.

## Assign complete questions

The lead gives each research question an owner, explains what is already known and what needs to be learned, and provides the relevant code, references, configuration and experiment records. That owner follows the question through investigation, implementation and analysis. Assign environment preparation separately so Explorers and Workers can proceed with source analysis, implementation and checks that do not need that environment.

Each assignment explains what may change, which workloads and generation settings must stay the same, and how much time and compute are available. Research tests uncertain benefits; a speedup need not be proven before implementation begins. Also name who will run the experiment, which checkout and environment to use, and where to save results. Members work independently within these agreements and ask the lead to coordinate changes to priorities, resource budgets, shared environments or comparison conditions.

Keep the complete proposal in `<output-dir>/iterations/<name>/notes/`, where `<output-dir>` is the experiment result directory. The [document collaboration guide](../../../../rsi-playbooks/experiment-records.md#share-documents-and-code) explains where to keep shared files and who writes them.

## Share findings and keep working

For example, an Explorer who finds a potentially faster method can send the findings and sources directly to the Worker studying that question. When the code is ready, the Worker asks the assigned Experimenter to test it. Reviewers also send specific issues to the person who can fix the code or run the missing comparison, without routing every exchange through the lead.

Use messages for short findings, questions and coordination. Put long explanations, full evidence or anything several people need to read in a shared file, then send a brief summary and its exact path. Give the background once and describe later changes without copying the whole document into messages. The [records guide](../../../../rsi-playbooks/experiment-records.md#share-documents-and-code) explains where files live and who writes them. Notify everyone when a change affects the whole team, and alert the executor immediately if an ongoing experiment will produce invalid results.

Observers gather evidence by reading work and session records rather than asking members for updates. They send their findings to the lead, who arranges any corrections; the team does not wait for the check to finish.

While waiting for a reply, continue work that does not need that answer. If progress really is blocked, explain what is missing and who can provide it. Routine updates do not need replies, and completion notifications remove the need to keep asking for status. Transfers of GPUs or running jobs still need someone to accept them. The lead follows the shared records and steps in when direction, scope or resources need to change.

Use session timelines to notice time spent repeating explanations, relaying messages, handing off work and relearning context. If communication displaces research or delays experiments, combine exchanges and use fewer, larger assignments with continuing owners. Judge the time spent and work delayed, not message count alone.

## Prepare candidates while evaluations run

The Worker prepares the version to test, and the Experimenter runs it; record that dependency in the tasks. With the code, supply the question to answer, reference results, commands, workload, GPU count and node placement. Explain which components need rebuilding or restarting and which processes and caches should remain in use, so the Experimenter can arrange the run directly.

The delivered checkout and execution environment stay unchanged while the candidate waits, builds or runs. Reuse the checkout after evaluation; create a separate development or evaluation copy only when further implementation needs to overlap those stages.

Keep the same Experimenter with a research direction where possible. In the team's shared task records, that Experimenter shows which approaches are still being prepared, which are ready to test and which are running, with links to completed results. The lead uses these records to arrange priorities and resources. If too many candidates are waiting, drop redundant comparisons while Workers continue research and analysis.

For example, five Workers can prepare approaches even when only two GPUs are available. The Experimenter runs two one-GPU experiments or one two-GPU experiment according to priority and available resources, while the other Workers continue. Record what is missing for work that cannot run yet. Arrange a measurement window for higher-priority multi-GPU work as devices become free. When GPUs are idle, check the records before asking Workers for missing versions or delivery estimates; involve the lead when the research schedule needs to change.

Give each candidate to one Experimenter so it is not run twice. If experiments need the same GPUs, or one needs GPUs from several groups, have one Experimenter arrange the order of use for those devices. Other groups continue independently. Agree who takes over before changing the executor. Keep this information in existing task tools, or in the iteration notes when no task tools are available.

## Coordinate resources and environments

With six suitable GPUs free, three two-GPU experiments can each use their own pair instead of taking turns. Prefer this arrangement within the task's budget. One Experimenter manages each group and may manage several groups. Across consecutive runs, keep the same devices and executor where possible so each group can deploy, measure and clean up independently. Queue experiments when there are not enough GPUs.

Before using GPUs, the Experimenter checks host processes and Kubernetes workloads, confirms that the task may use the devices and that no one else is using them, and records the devices used. Release GPUs when no experiment is ready to follow, and check availability again before reuse; do not reserve them for work that is still being prepared. Coordinate shared-platform and driver changes with the administrator.

Budget serving, builds, model/data preparation and benchmark clients against current node capacity and existing Pod requests. Adjust [native build concurrency](../../../../docs/deployment/source.md#edit-an-inference-engine) when needed.

Preparation and evaluation can overlap when resources allow. For final comparisons, agree on a measurement window with the owners of workloads sharing the nodes, storage or network. Finish or stop this task's competing workloads, then measure the reference and retained approaches under the same conditions.

When a measurement finishes, continue ready work or stop unused serving processes and release their GPU allocation; analysis and review can continue from saved results. Retain reusable images, build caches and model references. Running jobs and collectors may instead be handed to an owner who accepts them. After an SSH disconnect, inspect the original job before restarting it.

## Return results as work finishes

Existing sweeps can run fixed workload groups and repetitions continuously. Task-owned remote sessions or an existing job manager preserve logs and exit status so jobs can be checked after a disconnect. When reading results, distinguish whether measurement finished, whether requests succeeded and whether the task's requirements were met.

After each group of measurements, the Experimenter updates the records and resource state, sends the outcome and result links directly to the Worker and assigned reviewer, and continues already authorized work. The Worker interprets the measurements and advances the same research question. If priorities, budgets or comparison conditions need to change, the lead resolves that decision while unaffected work continues.

Experimenters maintain their iteration notes, and the lead assigns one writer to update the report. Summaries link to the original data so conclusions can be traced. Reviewed results remain useful while their inputs and conditions still apply. See the [experiment records guide](../../../../rsi-playbooks/experiment-records.md) for the record layout.

## Review claims independently

Before retaining an approach, the lead asks someone who did not implement or run it to check the results. The reviewer examines the actual code, runtime configuration and original data to confirm that the intended change ran. They then check precision, workload, output settings and resources, and consider whether failures, caches, warmup or competing work affected the comparison.

If a specific question remains unresolved, the reviewer explains what evidence or comparison would answer it. The Experimenter records that assessment separately from the developer's explanation. The lead then decides whether to investigate further, retain the approach or integrate it with other changes.

## Hand off and resume

When an assignment ends, the member reports the conclusion, result locations, open questions and resources still in use. Follow-up work can continue if it is already covered by the assignment; changes in scope or resources go to the lead. On resumption, check the records, remote jobs and team members before continuing with the original budget and completed results.

On a user pause, stop or deadline, the lead stops scheduling new experiments. Each owner stops or hands over their jobs and preserves the results. A GPU collector's deadline controls only that collector, so other jobs need their own stop action.

Count Workers, Experimenters, Explorers and Observers while they are actually doing assigned work, once per member. Observers count only during a check, not between rounds. Exclude the lead and members who are waiting, blocked or finished. Mark intervals with insufficient evidence as unknown.
