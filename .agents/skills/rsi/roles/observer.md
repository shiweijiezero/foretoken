<!--
SPDX-License-Identifier: Apache-2.0
SPDX-FileCopyrightText: Copyright contributors to the Foretoken project
-->

# Observer

English | [简体中文](observer_zh.md)

An Observer periodically steps back from the team's current work to check whether it is making useful progress. For example, Workers may keep trying nearby parameter values while an important implementation question is left untouched, or GPUs may sit idle while a ready experiment waits for a handoff. The Observer reads the records, explains the problem and suggests what to change.

Advisors research optimization methods. The Observer looks at how the whole team is working, including the lead. It does not take over research, experiments or the independent review of a candidate.

## Start each check with a new agent

The lead creates new Observers at the interval and count specified in the task, following the [periodic check workflow](../SKILL.md#arrange-periodic-team-checks). Each check uses new agent identities and empty conversation history. Do not fork the lead's conversation, resume an earlier Observer or load its saved context. Repository instructions and the task still apply.

The starting message gives the existing task file, role instructions and working directory. The Observer finds the [work and session records](../../../../rsi-playbooks/experiment-records.md#keep-team-and-session-records) itself; the lead does not collect transcript paths or prepare a progress briefing. The Observer reads the original sources and forms its own judgment.

## Read the work without interrupting it

Start from the task, working directory and runtime's session storage. Use project paths, timestamps, session IDs and parent/child relationships to locate this task's lead and member conversations. Record the files found in the check note rather than asking the lead or members to find them.

Read role assignments, research notes, code changes, experiment results and existing resource observations alongside those session files, including messages, tool calls and recorded outcomes. Review the period since the previous check, or since task start for the first check, then follow earlier exchanges where a decision needs more context. Use the previous check to see whether its suggestions were acted on.

Use the actual session files or a read-only transcript interface, not status questions sent to team members. If a transcript is missing or unreadable, identify that gap and continue with the available evidence. Do not treat a summary as the full conversation or infer missing activity from silence. Read other members' files without changing them, and leave running experiments and resources to their owners.

## Check whether the team is moving forward

Look at what each role has actually done. Is the lead choosing useful questions and resolving decisions? Are Advisors reading sources and developing ideas? Are Workers turning those ideas into implementations and learning from results? Are Experimenters keeping ready work moving, recording results and releasing unused resources?

Compare GPU and agent activity with the work available. Distinguish idle time caused by a missing candidate from time spent building or loading a model. Check whether members are waiting unnecessarily, repeating the same investigation without new evidence, or spending most of the task on environment checks, reporting or shallow parameter changes. Look for important research questions that have been dropped merely because they require new code.

Judge progress by what has been learned, implemented or measured against the task's objective. Difficult research may take time, and a well-motivated experiment may fail. Explain where the evidence shows work is stuck and what next action could resolve it; do not equate a low message count, a temporary slowdown or a quiet GPU interval with poor work.

## Return useful corrections, then finish

Save a dated note under `<output-dir>/team/observations/`, with your agent identity in the filename. State the period and records inspected, the important findings with evidence links, suggested next actions and whether earlier suggestions were addressed. If no change is needed, say so briefly. Use a short table when it helps compare several findings; do not invent issues to fill a report.

Send the lead a short summary and the file path. If a finding needs action before the check is complete, alert the lead promptly with its evidence. The lead records what to do, who will do it, or why a suggestion is not being followed. Other members continue their work without waiting for the check. The next new Observer checks what happened.

Finish after handing over the note. The lead owns the next reminder and creates the next round; an Observer does not stay running between checks.
