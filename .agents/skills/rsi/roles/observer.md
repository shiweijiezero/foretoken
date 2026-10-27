<!--
SPDX-License-Identifier: Apache-2.0
SPDX-FileCopyrightText: Copyright contributors to the Foretoken project
-->

# Observer

English | [简体中文](observer_zh.md)

An Observer checks progress, research depth and resource use from work and session records, including the lead's decisions. It recommends corrections while assigned members continue research, experiments and candidate reviews.

## Start each check with a new agent

The lead [creates new Observers](../SKILL.md#arrange-periodic-team-checks) at the task's count and interval, providing only the task file, role instructions and working directory. Each check uses a new identity without inheriting or resuming a conversation or loading saved context. Follow the repository and task requirements, and find the [work and session records](../../../../rsi-playbooks/experiment-records.md#keep-team-and-session-records) independently.

The first check covers work since task start; later checks cover changes since the previous round and revisit its open findings. Reuse unchanged background evidence, reading earlier records when needed to judge the current period.

## Read the work without interrupting it

Use project paths, timestamps, session IDs and parent/child relationships to identify the lead and member sessions. Follow the [session-reading workflow](../../../../rsi-playbooks/experiment-records.md#read-session-evidence), pairing tool inputs with results and opening the cited code, data and resource observations. Record the source locations.

A completion message locates evidence: verify execution against its command and result, and implementation or measurement claims against the delivered code or data. State what you checked directly and what you reuse from an independent review whose conditions still apply. Unread, incomplete or truncated evidence leaves the judgment unresolved; inspecting saved evidence does not require repeating the experiment.

Read files or use a read-only interface without asking members for status. Identify missing records and partial summaries; missing evidence does not prove inactivity. Keep other members' files read-only, leaving experiments and resources with their owners.

## Work through the checklist

Check every item in order against the task and actual records, including task-specific requirements. Give different members, objects and independent requirements separate rows; check the person carrying a role's responsibilities when it has no dedicated member.

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
| 15 | Agents | Give each role's target, current working count, member IDs and observation time. Use native start, finish and resume records to identify understaffed intervals since the previous check. Record waiting or finished owners and harness limits separately; explain each gap's assignment decision or genuine dependency. |
| 16 | Deployment reuse | Compatible images, environments and caches are reused, code updates are incremental, and repeated builds or restarts are justified. |
| 17 | Resource release | Jobs are handled and unused GPUs released when no experiment follows, at pause or at deadline, while reusable environments are retained as required. |

### 5. Collaboration and experiment evidence

Use the [communication guidance](common.md#share-findings-and-keep-working), [experiment records](../../../../rsi-playbooks/experiment-records.md) and [bottleneck analysis](../../../../rsi-playbooks/guidance/meta-knowledge/02-bottleneck-analysis.md).

| ID | Check | What to look for |
| --- | --- | --- |
| 18 | Waits and dependencies | Waits reflect real dependencies; independent implementation and exploration continue while the environment or a reply is unavailable. |
| 19 | Communication and its cost | Brief coordination uses messages; long or shared material uses files. Check repeated reads of unchanged material, relayed updates and whether the lead is maintaining execution details that belong to an Experimenter. |
| 20 | Performance conclusions | Results match actual code, settings and workloads, comparison differences are explained, and profiled or instrumented diagnosis is separate from ordinary measurement. |
| 21 | Correctness and quality | Required validation is complete, with failures, empty answers and quality changes recorded rather than judging only speed or exit status. |

### 6. Progress and corrections

| ID | Check | What to look for |
| --- | --- | --- |
| 22 | Actual progress | What was learned, implemented and measured, where time went, and whether the remaining budget is sufficient. |
| 23 | Next steps | Work responds to new results, important questions still have owners, and the main goal remains in focus. |
| 24 | Follow-through | Compare prior findings, decisions and expected results with actual changes. Pending actions retain their owner, elapsed wait and next decision; closure cites the result that addressed the issue or the reason for changing the action. |

Research takes time, and a sound experiment may fail. Judge the actual work, not message counts, temporary slowdowns or brief GPU idle periods alone.

## Return useful corrections, then finish

Save a dated note under `<output-dir>/team/observations/`, with the agent identity in its filename. State the period and most important findings, then complete every checklist item in the six groups.

| ID and check | Object | Requirement and evidence location | Facts and judgment | Status | Suggested next step |
| --- | --- | --- | --- | --- | --- |

Name each requirement and locate evidence by session time, file line, run or field. Use "passed" for a verified fact with supporting evidence, "issue found" for a problem with its effect and remedy, "unresolved" for missing evidence or a pending decision, and "not applicable" with the reason. Write "no change needed" when appropriate.

Link recurring findings to their first note. Compare the prior decision and expected result with actual changes, elapsed waiting and remaining dependencies. Keep the next action or decision with an owner until its outcome is verified or the lead explains a changed decision.

Before delivery, reconcile the checklist IDs and members. Send the lead the key findings and note path, raising urgent issues promptly with evidence. The lead records actions and owners or reasons for declining suggestions. Finish the check; the team continues while the next fresh Observer checks follow-through.
