---
name: rsi
description: Execute, resume, pause, or finish an explicitly requested Foretoken RSI optimization task. Use only when the user asks to run RSI, not for ordinary coding or discussion of RSI designs.
---

<!--
SPDX-License-Identifier: Apache-2.0
SPDX-FileCopyrightText: Copyright contributors to the Foretoken project
-->

# RSI in Claude Code

English | [简体中文](SKILL_zh.md)

Read `${CLAUDE_SKILL_DIR}/../../../.agents/skills/rsi/SKILL.md` and follow the shared workflow and role instructions.

Use an interactive Agent Team. Confirm Teams are enabled before creating members; otherwise follow the [session setup](../../../rsi-playbooks/README.md#claude-code). Named subagents in SDK or non-interactive sessions are not teammates.

This project enables Claude Code's Ultracode dynamic workflows in `.claude/settings.json`. Ultracode chooses when a substantive task needs a generated workflow; Agent Teams provide the long-lived peer sessions and direct communication. The task's model and effort settings remain authoritative.

The user sets one `/goal` in the initiating session for the task outcome and budget. Members own routine progress and delivery without separate goals. Native Teams provide member identity, direct communication and wake-up; the shared RSI work pool owns work assignment and dependencies. Do not maintain a second ownership or dependency record in native Task tools.

Follow the shared [continuation workflow](../../../.agents/skills/rsi/continuation.md) with `--harness claude`. Register native member IDs or teammate names only for continuing research and execution roles; the project `TeammateIdle` hook returns them to owned work or to claiming ready work without creating another teammate.

Give parallel writers task branches and independent checkouts with absolute paths. Prepare checkouts separately from team creation: passing `isolation` to the Agent tool creates an ordinary subagent rather than a teammate.

On resume, check the original budget, experiment records, jobs and members before recreating missing teammates. On pause or stop, have members stop task-owned jobs and preserve results; `/goal clear` cancels the goal but does not stop experiment processes.
