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

Read `${CLAUDE_SKILL_DIR}/../../../.agents/skills/rsi/SKILL.md` and follow its shared workflow and role contracts. Use an interactive Agent Team and the user's single lead `/goal`.

Before spawning members, check that the interactive session supports native Teams. Agent Teams are experimental and disabled by default; named subagents in SDK or non-interactive sessions remain ordinary subagents. If Teams are unavailable, use the dedicated-session instructions in the [playbooks](../../../rsi-playbooks/README.md#claude-code).
The user sets `/goal <completion condition>` in the lead session. Its condition includes maintaining useful tasks and the task-specified working-agent count during execution, responding to completion, blockage, and new-task requests, and delivering the optimization results at the task's end. Keep these as parts of one goal, not separate goals for each member. Members retain role context and receive concrete assignments; they do not need individual `/goal` commands. The lead develops directions, assigns work, and synthesizes decisions, owning environment preparation, deployment and parallel experiments, and final acceptance. Workers research and develop candidates and analyze returned results; follow the shared role contracts for execution and ongoing collaboration.

For parallel candidates, the lead creates Git worktrees first and passes their absolute paths in assignments. Workers edit assigned candidates; the lead runs each delivered version from its corresponding checkout. Keep source unchanged during execution, using another worktree for further development or waiting until the lead releases the checkout. Claude Code treats an Agent spawn with explicit `isolation` as a subagent rather than a teammate; keep teammate spawning and checkout preparation separate.

Use the shared role contract for assignment authority and handoffs. Intermediate results inform ongoing work; completed assignments and changes of scope need the lead's attention. Shared Task tools, when available, can track ownership and dependencies. Choose suitable monitoring and continuation tools for the task; `/goal` may defer evaluation while background work is running.

On resume, recover the task from its records and original budget, check live team membership, and recreate missing teammates before assigning unfinished work. A restored lead goal does not restore in-process teammates. User pause/stop and the task deadline override further assignments; coordinate owned cleanup and delivery. The user can cancel the active goal with `/goal clear`; clearing it does not itself stop experiment processes.
