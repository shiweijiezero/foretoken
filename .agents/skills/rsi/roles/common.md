<!--
SPDX-License-Identifier: Apache-2.0
SPDX-FileCopyrightText: Copyright contributors to the Foretoken project
-->

# Working as an RSI team

English | [简体中文](common_zh.md)

Workers can prepare the next approach while Experimenters evaluate the previous one. Results return directly to the developers, and the lead uses them to adjust research priorities and resources. Keep related work with the same members so they can build on what they have learned.

## Assign complete questions

Each member reads the task, role instructions, relevant code and previous results. The lead provides those sources and explains:

- the question to answer and the findings or decisions that already apply;
- what may change and which workloads, precision and generation settings must stay comparable;
- who will run the experiment, which checkout, deployment and resources it will use, and where results belong;
- the remaining budget, follow-up work that can proceed, and changes that need coordination.

Members continue independently within that assignment rather than asking for every command. The first update should identify the reference and planned comparison so misunderstandings surface early. Additional resources, shared environment changes and different comparison conditions go back to the lead.

## Prepare candidates while evaluations run

When different members develop and evaluate a candidate, use two tasks with a dependency between them. The Worker supplies a reproducible code version, change explanation, reference result, workload and execution requirements. Keep the delivered version unchanged while it waits or runs; use another checkout for further development or preserve a separate evaluation copy.

Use the same task records to track candidates being prepared, waiting for evaluation and executing. Choose preparation and evaluation concurrency separately. If preparation leaves evaluation resources idle, prepare useful candidates ahead. If the queue grows, reduce new preparation and spend the time interpreting results or improving work still in development. Adjust priorities from observed durations, remaining time and new findings, recording why a candidate is deferred or dropped.

Use native task owners, dependencies and completion records when available. Members may self-claim only when the harness supports atomic claims and the lead has assigned the required resources. Otherwise the lead assigns work. Without task tools, keep assignments and progress in the experiment overview.

## Coordinate resources and environments

One member runs and cleans up each deployment. The lead allocates resources within the user's authorization and coordinates changes to shared controllers, CRDs, drivers and platform installations. Assign code ownership by checkout, file or section to avoid concurrent edits to the same content.

Use Kubernetes requests, limits and device plugins for managed model workloads. Capacity planning also needs to cover builds, preparation jobs and benchmark clients. Separate namespaces and caches prevent data conflicts, but workloads on one node can still compete for CPU, storage and network capacity. Before a comparison that requires isolation, the affected members confirm that competing work has stopped.

An executor may continue with the next authorized candidate in the same allocation. Release resources after their processes and services stop or another owner accepts the handoff. After an SSH disconnect, check the original job before starting a replacement.

## Return results as work finishes

Run fixed workload groups and repetitions through existing sweeps. Use task-owned remote sessions or an existing job manager to retain logs and exit status. Read saved benchmark results to distinguish measurement completion, request outcomes and acceptance against the task's requirements.

Send new results directly to the people who need them. Include the result location, resource state, next action and any decision needed. On completion or a dependency change, the lead schedules ready work before returning to its own research. Members continue unaffected work when part of an assignment is blocked.

Experimenters maintain their iteration notes, and one designated writer updates the overview and report. Summaries link to the original data. Reuse reviewed results when their inputs and conditions still apply. See the [experiment records guide](../../../../rsi-playbooks/experiment-records.md) for the record layout.

## Review claims independently

Before retaining an approach, ask someone who did not implement or run it to check the results. Review the actual code, runtime configuration and original data. Confirm that the change took effect and check precision, workload, output settings and allocated resources. Consider whether failed requests, caches, warmup or competing load explain the apparent gain. Request further measurements only to answer a specific unresolved question.

Keep the developer's explanation, reviewer's assessment and lead's decision distinct in the records. The Experimenter brings these contributions together; the lead decides what to retain or integrate.

## Hand off and resume

At the end of an assignment, report the conclusion, result locations, open questions and resources still in use. Continue follow-up work already covered by the assignment, or propose it to the lead. On resumption, check recorded progress, live jobs and team members before continuing with the original budget and completed results.

On a user pause, stop or deadline, stop scheduling new experiments. Each owner stops or hands over their jobs and preserves the results. A GPU collector's deadline stops only that collector; other jobs need their own stop action.

Count Workers, Experimenters and Advisors while they are actually doing assigned work, once per member. Exclude the lead and members who are waiting, blocked or finished. Mark intervals with insufficient evidence as unknown.
