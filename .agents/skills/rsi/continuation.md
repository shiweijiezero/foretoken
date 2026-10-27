<!--
SPDX-License-Identifier: Apache-2.0
SPDX-FileCopyrightText: Copyright contributors to the Foretoken project
-->

# Continue RSI assignments

English | [简体中文](continuation_zh.md)

Project hooks help members finish the next part of an assigned research question and remind the lead to arrange follow-up work. They act only on explicitly activated RSI sessions. Ordinary development, reviews and chats have no binding and continue unchanged.

## Enable the task

Use Python 3.11+ and the hooks shipped in the checkout. Codex discovers `.codex/hooks.json`; review and trust these definitions through `/hooks`. Claude Code loads `.claude/settings.json` for the project. Neither setup installs hooks globally. See the native [Codex](https://learn.chatgpt.com/docs/hooks) and [Claude Code](https://code.claude.com/docs/en/hooks) hook instructions.

For Codex linked worktrees, hook definitions come from the primary checkout. Keep the hook configuration there and the continuation script in each task checkout, then use `/hooks` to confirm which definitions are loaded. A hook file added only in a linked worktree is not discovered by Codex 0.162.1.

The lead activates continuation after establishing the task and its original deadline. Set `RSI_HARNESS` to `codex` or `claude`, `RSI_SESSION` to the native root session ID, and `RSI_TASK` to the task file. Set `RSI_DEADLINE` to the task's ISO deadline with a UTC offset, then run from a task checkout:

```bash
python -I .agents/skills/rsi/scripts/continuation.py activate \
  --harness "$RSI_HARNESS" --session "$RSI_SESSION" \
  --task "$RSI_TASK" --deadline "$RSI_DEADLINE"
```

Omit `--deadline` for a task without a time budget. Repeat activation to resume a paused task with its original deadline; completed tasks stay closed. The returned state describes hook bindings, not the harness's actual active-agent count.

## Arrange a member's next step

For example, a Worker handing a candidate to an Experimenter can next investigate an unresolved part of the call path. Register that concrete, authorized step before the member ends its current stage. Set `RSI_AGENT` to its native ID, `RSI_ROLE` to `explorer`, `worker` or `experimenter`, and `RSI_NEXT_STEP` to the next question and its relevant source paths:

```bash
python -I .agents/skills/rsi/scripts/continuation.py assign \
  --harness "$RSI_HARNESS" --session "$RSI_SESSION" \
  --agent "$RSI_AGENT" --role "$RSI_ROLE" --next-step "$RSI_NEXT_STEP"
```

Claude teammates may use their explicit teammate name. Use identities returned by the harness or its session metadata, not a display label. Observers and one-time review agents remain unregistered and end normally.

At `SubagentStop`, the hook consumes the registered step and asks the same member to continue. Claude's `TeammateIdle` uses the same pending work. A step is delivered once; another stop does not repeat it. When a registered member finishes without another step, the lead receives one reminder at its next `Stop` to check role targets and arrange work through the native agent tools.

Use `status` with the same harness and root session to inspect pending steps. A hook does not create members or decide whether new research is useful; those decisions remain with the lead.

## Wait, pause or finish

When progress depends on an unavailable result, release the member as `waiting`; use `done` when its question is answered. Set `RSI_REASON` to the outcome, or the missing evidence and condition for resuming:

```bash
python -I .agents/skills/rsi/scripts/continuation.py release \
  --harness "$RSI_HARNESS" --session "$RSI_SESSION" \
  --agent "$RSI_AGENT" --state waiting --reason "$RSI_REASON"
```

The lead then assigns another useful question or records why the role is temporarily unfilled. Waiting or finished members receive no further continuation until explicitly assigned work.

Before pausing or stopping the task, release the root binding with `--state paused`, omitting `--agent`. Use `--state complete` on completion. Pending steps are cleared; experiment and collector cleanup still follows the RSI workflow. The hook also stops continuing work at the deadline or session end; Codex's `Interrupt` pauses the binding. On resume, activate the same task and register the next work again.

Bindings are stored once in the repository's Git common directory, shared by linked worktrees and separated by harness and root session. Experiment records remain in their existing locations. Keep the root session and member identities with the team assignments so a resumed lead can manage the same bindings.
