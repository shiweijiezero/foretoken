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

The user sets one `/goal` in the lead session for the task outcome, team progress and delivery. Members do not need separate goals. Use shared Task tools for owners and dependencies when available, and exchange results through messages.

Give parallel writers task branches and independent checkouts with absolute paths. Prepare checkouts separately from team creation: passing `isolation` to the Agent tool creates an ordinary subagent rather than a teammate.

On resume, check the original budget, experiment records, jobs and members before recreating missing teammates. On pause or stop, have members stop task-owned jobs and preserve results; `/goal clear` cancels the goal but does not stop experiment processes.
