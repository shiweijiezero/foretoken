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

The lead begins an assignment by explaining the question, the findings already available and the decision the work should inform. It provides the relevant code, reference configuration and experiment records so the member can read the sources and understand their conditions and unresolved questions.

Members also need to know what may change, which workloads and generation settings must remain comparable, and how much time and resource they can use. Every experiment has a named executor, checkout, deployment environment and result location. With those arrangements in place, members can continue without approval for each command. They return to the lead when the next step needs additional resources, a shared environment change or different comparison conditions.

The first update should explain the chosen reference and planned comparison. This gives the lead a chance to catch misunderstandings before implementation and measurement are complete.

## Prepare candidates while evaluations run

When different members develop and evaluate an approach, record them as two tasks with a dependency between them. The Worker provides a reproducible version, explains the change and expected effect, and supplies the reference results, workload and execution requirements. These inputs let the Experimenter determine how to deploy and evaluate the candidate.

The delivered version stays unchanged while it waits or runs. The Worker can develop in a different checkout or preserve a separate copy for evaluation before continuing. This keeps the measured version consistent with the handoff.

The lead uses one set of task records to track candidates being prepared, waiting for evaluation and executing. Preparation and evaluation concurrency can be adjusted separately. When preparation is slow and evaluation resources often sit idle, Workers can prepare a small set of useful, independent candidates ahead. When the queue is already long, they can spend more time interpreting results or improving work not yet delivered. The lead also accounts for the time needed to evaluate and analyze candidates before the budget ends.

Use native task owners, dependencies and completion states when available. Members may self-claim when the tools prevent duplicate claims and the lead has assigned the necessary resources. Otherwise, the lead assigns an owner explicitly. Without task tools, keep these arrangements in the experiment overview.

## Coordinate resources and environments

One member is responsible for running and cleaning up each deployment. The lead allocates resources within the user's authorization and coordinates changes to CRDs, drivers and shared platform installations. Code changes also need clear owners and scope so members do not overwrite one another's files.

Use Kubernetes requests, limits and device plugins to place managed model workloads, while also budgeting for builds, preparation jobs and benchmark clients. Separate namespaces and caches prevent data conflicts, but workloads on one node may still compete for CPU, storage and network capacity. Before a comparison that requires isolation, the affected members therefore confirm that competing work has stopped.

An executor can continue with another authorized candidate in the same allocation. Resources become available to another task only after their processes and services stop or the receiving member accepts the handoff. After an SSH disconnect, inspect the original remote job before deciding whether to start it again.

## Return results as work finishes

Existing sweeps can run fixed workload groups and repetitions continuously. Task-owned remote sessions or an existing job manager preserve logs and exit status so jobs can be checked after a disconnect. When reading results, distinguish whether measurement finished, whether requests succeeded and whether the task's requirements were met.

After each group of measurements, the Experimenter sends the results directly to the members who need them, along with their location, resource state and proposed next action. If the lead needs to decide something, explain why. The lead handles resulting scheduling changes before returning to its own research, while members continue unaffected work when part of an assignment is blocked.

Experimenters maintain their iteration notes, and the lead assigns one writer to update the overview and report. Summaries link to the original data so conclusions can be traced. Reviewed results remain useful while their inputs and conditions still apply. See the [experiment records guide](../../../../rsi-playbooks/experiment-records.md) for the record layout.

## Review claims independently

Before retaining an approach, the lead asks someone who did not implement or run it to check the results. The reviewer examines the actual code, runtime configuration and original data to confirm that the intended change ran. They then check precision, workload, output settings and resources, and consider whether failures, caches, warmup or competing work affected the comparison.

If a specific question remains unresolved, the reviewer explains what evidence or comparison would answer it. The Experimenter records that assessment separately from the developer's explanation. The lead then decides whether to investigate further, retain the approach or integrate it with other changes.

## Hand off and resume

When an assignment ends, the member reports the conclusion, result locations, open questions and resources still in use. Follow-up work can continue if it is already covered by the assignment; changes in scope or resources go to the lead. On resumption, check the records, remote jobs and team members before continuing with the original budget and completed results.

On a user pause, stop or deadline, the lead stops scheduling new experiments. Each owner stops or hands over their jobs and preserves the results. A GPU collector's deadline controls only that collector, so other jobs need their own stop action.

Count Workers, Experimenters and Advisors while they are actually doing assigned work, once per member. Exclude the lead and members who are waiting, blocked or finished. Mark intervals with insufficient evidence as unknown.
