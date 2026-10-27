<!--
SPDX-License-Identifier: Apache-2.0
SPDX-FileCopyrightText: Copyright contributors to the Foretoken project
-->

# Continue RSI assignments

English | [简体中文](continuation_zh.md)

Project hooks help members finish the next part of an assigned research question and remind the lead to arrange follow-up work. They act only on explicitly activated RSI sessions; unbound sessions continue unchanged. Each root session belongs to one RSI task. Use a new root session for a different task.

## Enable the task

Use Python 3.11+ and the hooks shipped in the checkout. Codex discovers `.codex/hooks.json`; review and trust the project definitions under Settings → Hooks in the desktop app, or through `/hooks` in the terminal CLI. Claude Code loads `.claude/settings.json` for the project. Neither setup installs hooks globally. See the native [Codex](https://learn.chatgpt.com/docs/hooks) and [Claude Code](https://code.claude.com/docs/en/hooks) hook instructions.

For Codex linked worktrees, keep the hook configuration in the primary checkout and the continuation script in each task checkout. Older checkouts without the script skip these hooks. Check the project source path in Hooks settings or the CLI's `/hooks` view to confirm which definitions are loaded.

The lead activates continuation after establishing the task and its original deadline. Set `RSI_HARNESS` to `codex` or `claude`, `RSI_SESSION` to the native root session ID, and `RSI_TASK` to the task file. Set `RSI_DEADLINE` to the task's ISO deadline with a UTC offset, then run from a task checkout:

```bash
python -I .agents/skills/rsi/scripts/continuation.py activate \
  --harness "$RSI_HARNESS" --session "$RSI_SESSION" \
  --task "$RSI_TASK" --deadline "$RSI_DEADLINE"
```

Omit `--deadline` for a task without a time budget. Repeat activation to resume a paused task with its original deadline; completed tasks stay closed. The returned state describes hook bindings, not the harness's actual active-agent count.

## Arrange a member's next step

For example, a Worker implementing a candidate can later investigate an unresolved part of the call path while the candidate is measured. Use `queue` to record that later step while the member is running. Set `RSI_AGENT` to its native ID, `RSI_ROLE` to `explorer`, `worker` or `experimenter`, and `RSI_NEXT_STEP` to the next question and its relevant source paths:

```bash
python -I .agents/skills/rsi/scripts/continuation.py queue \
  --harness "$RSI_HARNESS" --session "$RSI_SESSION" \
  --agent "$RSI_AGENT" --role "$RSI_ROLE" --next-step "$RSI_NEXT_STEP"
```

Use the member's native ID returned by the harness. If Codex returns only a task name, the member can include its own `CODEX_THREAD_ID` in its first progress update. Keep `RSI_SESSION` set to the lead's ID when registering that member. Claude teammates may use their explicit teammate name. Observers and one-time review agents remain unregistered and end normally.

At `SubagentStop`, the hook consumes the registered step and asks the same member to continue. Claude's `TeammateIdle` uses the same pending work. A step is delivered once; another stop does not repeat it. When a registered member finishes without another step, the lead receives one reminder at its next `Stop` to check role targets and arrange work through the native agent tools.

Use `status` with the same harness and root session to inspect pending steps. `queue` does not start or resume an agent. Give an idle member its current assignment through the harness's native follow-up tool; queue only a different step to run afterward.

## Wait, pause or finish

When progress depends on an unavailable result, release the member as `waiting`; use `done` when its question is answered. Set `RSI_REASON` to the outcome, or the missing evidence and condition for resuming:

```bash
python -I .agents/skills/rsi/scripts/continuation.py release \
  --harness "$RSI_HARNESS" --session "$RSI_SESSION" \
  --agent "$RSI_AGENT" --state waiting --reason "$RSI_REASON"
```

The lead then resumes the member through the native tools with another useful question, or records why the role is temporarily unfilled. Register a further step with `queue` when one is ready.

Before pausing the task or using the same chat for ordinary work, release the root binding with `--state paused`, omitting `--agent`. Use `--state complete` on completion. Pending steps are cleared; experiment and collector cleanup still follows the RSI workflow. The hook also stops continuing work at the deadline or session end; Codex's `Interrupt` pauses the binding. On resume, activate the same task, resume members through native tools, and queue further work as needed.

Bindings are stored once in the repository's Git common directory, shared by linked worktrees and separated by harness and root session. Experiment records remain in their existing locations. Keep the root session and member identities with the team assignments so a resumed lead can manage the same bindings.
