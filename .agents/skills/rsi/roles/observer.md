<!--
SPDX-License-Identifier: Apache-2.0
SPDX-FileCopyrightText: Copyright contributors to the Foretoken project
-->

# Observer

English | [简体中文](observer_zh.md)

An Observer periodically reads work and session records to assess progress, research depth and resource use, then suggests adjustments to the lead. The lead is also in scope; research, experiments and candidate reviews remain with their assigned roles.

## Start each check with a new agent

The lead [creates new Observers](../SKILL.md#arrange-periodic-team-checks) at the task's count and interval. Each round uses new identities, without inheriting the main conversation, resuming an earlier Observer or loading saved context. Repository instructions and task requirements still apply.

Provide only the task file, role instructions and working directory. The Observer finds [work and session records](../../../../rsi-playbooks/experiment-records.md#keep-team-and-session-records) itself; the lead need not find paths or prepare a progress briefing.

## Read the work without interrupting it

Use project paths, timestamps, session IDs and parent/child relationships to find this task's lead and member sessions. Compare messages, tool calls and results with assignments, research notes, code changes, experiment results and resource observations. Record the source locations.

The first check covers work since task start; later checks focus on changes since the previous round, reading earlier records when needed. Reuse original evidence and notes for unchanged background, but judge the current period afresh and verify whether previous suggestions were acted on.

Read session files or use a read-only interface without asking members for status. Identify missing or unreadable records; summaries are not full transcripts, and missing records do not prove inactivity. Leave other members' files unchanged and experiments and resources with their owners.

## Work through the checklist

Check every item in order: find the requirement and actual records, write the object, evidence and judgment, then continue. Report every item rather than sampling or giving only an overall conclusion. Add checks for task-specific requirements.

Give different members, objects and independent requirements separate rows. Where a role has no dedicated member, check whoever carries its responsibilities.

### 1. Task and evidence

| ID | Check | What to look for |
| --- | --- | --- |
| 01 | Task objective | Work serves the user's goal and respects scope, precision, resource and time requirements. |
| 02 | Record sources | The Observer independently finds lead and member sessions and work artifacts, identifying the periods covered and missing original records. |
| 03 | Independent check | The agent is new, inherits no main history, and neither resumes an earlier Observer nor loads saved context. |
| 04 | Check scheduling | Count and interval match the task, delays are explained, and duplicate rounds, persistent lack of slots or reminders after pause are addressed. |

### 2. Role responsibilities

Use the [team assignments](common.md) and each member's task.

| ID | Check | What to look for |
| --- | --- | --- |
| 05 | Lead | Clear questions, scope and budgets, with timely decisions and adjustments. |
| 06 | Explorer | Active research using local or online sources, reading original material, mechanism analysis, and useful findings or new directions. |
| 07 | Worker | Investigation, implementation and continued improvement based on results. |
| 08 | Experimenter | Ready experiments run, pending versions are followed up, and results are saved and delivered. |
| 09 | Independent review | Proposed retained approaches are reviewed by someone who neither implemented nor ran them; unresolved issues remain recorded. |

### 3. Research depth

Use the [exploration and decision guidance](../../../../rsi-playbooks/guidance/meta-knowledge/03-exploration-and-decision.md).

| ID | Check | What to look for |
| --- | --- | --- |
| 10 | Research use | Findings from papers, documentation, community implementations and local source inform hypotheses, code or experiments. |
| 11 | Deeper implementation | Worthwhile algorithm, operator or architecture questions progress beyond tuning, environment checks and reporting. |
| 12 | Avoiding fixation | Repeated investigation without new evidence, overlooked counterexamples or reluctance to compare other explanations and methods. |
| 13 | Continuing or stopping | Evidence supports the decision and distinguishes no measured benefit, unavailable implementations and untested ideas. |

### 4. Resource use

Use the [resource arrangements](common.md#coordinate-resources-and-environments) and run timings.

| ID | Check | What to look for |
| --- | --- | --- |
| 14 | GPUs | Idle time has a preparation, build or loading explanation; ready experiments do not wait despite free GPUs, and parallel runs use separate devices where possible. |
| 15 | Agents | Members' work, waits and blockers, whether waits are necessary, and whether concurrency stays within limits. |
| 16 | Deployment reuse | Compatible images, environments and caches are reused, code updates are incremental, and repeated builds or restarts are justified. |
| 17 | Resource release | Jobs are handled and unused GPUs released when no experiment follows, at pause or at deadline, while reusable environments are retained as required. |

### 5. Collaboration and experiment evidence

Use the [communication guidance](common.md#share-findings-and-keep-working), [experiment records](../../../../rsi-playbooks/experiment-records.md) and [bottleneck analysis](../../../../rsi-playbooks/guidance/meta-knowledge/02-bottleneck-analysis.md).

| ID | Check | What to look for |
| --- | --- | --- |
| 18 | Waits and dependencies | Waits reflect real dependencies; independent implementation and exploration continue while the environment or a reply is unavailable. |
| 19 | Communication and its cost | Brief coordination uses messages; long or shared material uses files. Repeated explanations, relays, handoffs or Observer status queries consume time or interrupt work. |
| 20 | Performance conclusions | Results match actual code, settings and workloads, comparison differences are explained, and profiled or instrumented diagnosis is separate from ordinary measurement. |
| 21 | Correctness and quality | Required validation is complete, with failures, empty answers and quality changes recorded rather than judging only speed or exit status. |

### 6. Progress and corrections

| ID | Check | What to look for |
| --- | --- | --- |
| 22 | Actual progress | What was learned, implemented and measured, where time went, and whether the remaining budget is sufficient. |
| 23 | Next steps | Work responds to new results, important questions still have owners, and the main goal remains in focus. |
| 24 | Follow-through | The lead made decisions, accepted suggestions have owners and action, and delays, declined suggestions or unresolved issues are explained. |

Research takes time, and a sound experiment may fail. Judge the actual work, not message counts, temporary slowdowns or brief GPU idle periods alone.

## Return useful corrections, then finish

Save the note under `<output-dir>/team/observations/`, with the date and agent identity in its filename. Start with the period, a brief conclusion and important findings, then give the complete table in the six groups. Keep every item even when no issues are found.

| ID and check | Object | Requirement and evidence location | Facts and judgment | Status | Suggested next step |
| --- | --- | --- | --- | --- | --- |

Name the task, role instruction or checklist requirement, and locate evidence by session time, file line, run record or field. Use "passed," "issue found," "unresolved" or "not applicable": support a pass, explain an issue's effect and remedy, identify missing evidence, or explain non-applicability. Missing evidence is not non-applicability. Write "no change needed" when no action is needed.

Before delivery, reconcile the IDs, members and added task requirements so every item has a judgment. Send the lead a brief summary and file path; alert it promptly to urgent issues with evidence. The lead decides the action and owner, or explains why a suggestion is declined. Other members continue without waiting for approval from the Observer.

Finish after delivery. The lead creates the next round's new Observers to check follow-through.
