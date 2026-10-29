<!--
SPDX-License-Identifier: Apache-2.0
SPDX-FileCopyrightText: Copyright contributors to the Foretoken project
-->

# Continue RSI work

English | [简体中文](continuation_zh.md)

Task-scoped work is shared by native members through `scripts/continuation.py`. Members claim ready questions or informative stages and deliver evidence directly, without a dispatching agent. Existing experiment notes hold the detailed brief and original artifacts.

## Enable the task

Use Python 3.11+ and the project's trusted hooks. Codex discovers `.codex/hooks.json`; review it in Settings → Hooks or `/hooks`. Claude Code loads `.claude/settings.json`. For linked Codex worktrees, keep hook configuration in the primary checkout and the script in each task checkout; check which definitions the harness loaded.

At initialization, set `RSI_HARNESS` to `codex` or `claude`, `RSI_SESSION` to the task's native root ID, `RSI_TASK` to its task file and `RSI_DEADLINE` to its original ISO deadline with a UTC offset. Run from a task checkout:

```bash
python3 -I .agents/skills/rsi/scripts/continuation.py activate \
  --harness "$RSI_HARNESS" --session "$RSI_SESSION" \
  --task "$RSI_TASK" --deadline "$RSI_DEADLINE"
```

Omit the deadline only for an unbounded task. Repeat activation to resume with the same task and original deadline; a completed task remains closed. New tasks record their start automatically. When adopting an older binding without a start time, use `--started-at` with the original ISO start before configuring checks. The shared SQLite file is in the repository's Git common directory, separated by harness and task session. It records work ownership, not GPU allocation or native agent activity.

## Register and claim work

Each continuing member sets `RSI_AGENT` to its own native ID and registers its role. For example, a Worker runs:

```bash
python3 -I .agents/skills/rsi/scripts/continuation.py register \
  --harness "$RSI_HARNESS" --session "$RSI_SESSION" \
  --agent "$RSI_AGENT" --role worker \
  --thread-id "$NATIVE_THREAD_ID" \
  --responsibility "$RSI_RESPONSIBILITY"
python3 -I .agents/skills/rsi/scripts/continuation.py claim \
  --harness "$RSI_HARNESS" --session "$RSI_SESSION" \
  --agent "$RSI_AGENT" --wait 30
```

Roles are `explorer`, `worker` and `experimenter`. Each can also claim independent review and periodic-check dispatch. Use repeated `--accept` arguments for explicitly combined responsibilities when dedicated staffing is absent. Fresh Evaluators do not register as continuing members.

Claim when there is available capacity. A claim atomically selects eligible ready work, reusing topic and environment context while giving older work its turn before newer arrivals. Unfinished dependencies and reviewer exclusions prevent a claim; work is not reserved for a busy member. `--wait` waits inside the tool, releasing SQLite between short checks.

Without an eligible item, every continuing member receives `action: research`, including when `--wait` is set. Continue from the current retained configuration and unresolved task questions rather than waiting for another producer; Workers and Experimenters can submit the next implementation or measurement stage themselves. An empty claim is not completion of a research responsibility.

## Arrange a member's next step

Keep the full brief in existing iteration notes. Set `RSI_WORK` to a unique work name, `RSI_BRIEF` to the existing brief path, and `RSI_TOPIC` and `RSI_ENVIRONMENT` to its topic and reusable environment. Submit the next question or stage:

```bash
python3 -I .agents/skills/rsi/scripts/continuation.py submit \
  --harness "$RSI_HARNESS" --session "$RSI_SESSION" \
  --agent "$RSI_AGENT" --work "$RSI_WORK" --kind experimenter \
  --description "Measure the candidate described in $RSI_BRIEF" \
  --topic "$RSI_TOPIC" --environment "$RSI_ENVIRONMENT"
```

Kinds are `explorer`, `worker`, `experimenter`, `review` and `evaluation`. Topic and environment describe useful context affinity, not an exclusive executor. Repeated `--after WORK` arguments refer to existing work that must finish first. The consumer receives those result paths and reads their actual findings, including failed experiments.

For review work, link implementation and execution items with `--after`. Their actual Worker and Experimenter participants, including previous owners after a handoff, cannot claim that review. An Explorer may review an exploration ancestor, but if it implemented or ran the candidate, add that identity with `--exclude ID`. Independence still depends on the member's real participation and the task's review requirements.

Finish owned work with `finish --agent "$RSI_AGENT" --work "$RSI_WORK" --result "$RSI_RESULT"`, including the same harness/session arguments. The result must be an existing artifact or analysis; its contents state the outcome. Publish follow-on work as useful evidence arrives rather than waiting for a whole matrix. Use native follow-up when a known idle consumer needs to resume. Shared work admission and native agent dispatch are separate operations.

The response to `submit`, `finish`, `handoff` and legacy `queue` includes compact `notifications` routes when a preferred familiar member or another idle qualified member can act. The producer sends that route through native follow-up or direct member messaging; it is targeted and never broadcast. Members can still claim the ready item when no route is delivered.

Use `handoff --agent "$RSI_AGENT" --work "$RSI_WORK" --reason "$RSI_REASON"` with the same harness/session to return the same item for another member. The reason locates the original live jobs, artifacts and next recovery action. The successor inspects them instead of treating the handoff as a new experiment. If the previous native owner has stopped, a peer can use `handoff --from-agent EXPECTED_OWNER`, recording that evidence and the original jobs in the reason. It changes ownership only if that owner still holds the item; the peer then claims eligible work normally. `status` shows work, ownership and results. Legacy `queue` replaces that member’s unclaimed next step in the shared pool. Active work and handed-off recovery work remain unchanged; the pending step is not reserved for its named member.

## Periodic checks

Configure `schedule --interval 3600 --count 1` with the harness/session for an hourly fresh check, using the task's actual interval and count. Count 0 cancels unclaimed checks; an already running assessment stays with its owner until finished. Rescheduling preserves the original anchor. A claim makes a due `evaluation` dispatch item available; missed rounds do not accumulate while a batch remains unfinished.

An available member claims the dispatch, creates a fresh Evaluator with only the task, role and working directory, and receives its assessment path. Finish the dispatch item with that actual saved assessment, not a launch notice. The Evaluator sends actionable findings directly to their owners. The pool creates no permanent Evaluator scheduler or report writer; the separate Agent Scheduler below only wakes continuing member threads.

This pool schedule is the only evaluation timetable. A live member's idle hook or claim materializes a due dispatch, and an available member consumes it; it is not a daemon and cannot dispatch after the harness exits. The Agent Scheduler below is a native-thread wake-up mechanism, not a second evaluation schedule. Do not create another evaluation timetable through reminders.

## Run the independent Agent Scheduler

The scheduler uses the stable Codex app-server `thread/read`, `thread/resume` and `turn/start` lifecycle ([app-server reference](https://learn.chatgpt.com/docs/app-server)).

Hooks and `queue` only persist work; they do not wake an idle native thread. For a task that must continue after the lead turn ends, run one independent Agent Scheduler against the Codex app-server:

```bash
python3 -I .agents/skills/rsi/scripts/agent_scheduler.py \
  --harness codex --session "$RSI_SESSION" --cwd "$PWD" --interval 2
```

Without `CODEX_APP_SERVER_SOCKET`, the scheduler starts a dedicated stdio app-server. Set that variable only when an existing app-server control socket is intentionally managed by the host.

Each registered member supplies a stable `--thread-id` and a complete `--responsibility`. The scheduler reads `thread/read` status, leases a `pending` responsibility in SQLite, calls `thread/resume`, and marks it `running` only after `turn/start` returns a turn ID. A completed or idle thread returns to `pending`; an active thread is never started twice. Lease expiry permits another scheduler instance to recover a crashed scheduler. Delivery is at-least-once across an ambiguous process failure, so every prompt contains a dispatch ID and the member must inspect existing work before acting.

The scheduler stops when the bound session is paused, complete or expired. It must also be stopped explicitly at the task deadline; `SessionEnd` records durable state but does not pause the RSI or create new work. Member release states are `waiting`, `done`, `paused`, `expired` or an explicit failure/resource conflict; `idle` and `needs_assignment` are scheduler faults, not normal research states.

## Wait, pause or finish

If no ready work can resolve a dependency, use `release --agent "$RSI_AGENT" --state waiting --reason "$RSI_REASON"` with the harness/session. Finish or hand off owned items before releasing. Use `done` when the responsibility is complete. A producer resumes a known waiting consumer with native follow-up when its result arrives.

Member stop hooks return unfinished members to their owned work or to claiming ready work. Codex uses `SubagentStop`; Claude uses `TeammateIdle`. Codex `Stop` and `PostToolUse` remain root fallbacks that materialize a due Evaluator dispatch, so a missed member-stop event is recovered by another live event. A guarded member stop records a root notice instead of silently dropping active work; the next root `PostToolUse` adds non-blocking context, while a root `Stop` consumes the notice once and requests one review turn. `SessionEnd` only leaves durable state and never creates new work because the main thread is ending. This does not make native agents independent of the harness lifetime.

On user pause or completion, use `release --state paused` or `release --state complete` with the harness/session and a reason, omitting the member ID. A root Interrupt pauses the task, and the original deadline stops new claims. Each owner still stops its own serving, build and collector jobs and verifies exit. On resumption, inspect existing jobs, activate the same task and resume members through native tools.
