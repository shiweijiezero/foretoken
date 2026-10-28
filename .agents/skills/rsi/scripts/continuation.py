# SPDX-License-Identifier: Apache-2.0
# SPDX-FileCopyrightText: Copyright contributors to the Foretoken project

"""Continue RSI assignments and deliver pending decisions at native tool boundaries."""

from __future__ import annotations

import argparse
import json
import os
import sqlite3
import subprocess
import sys
from contextlib import closing
from datetime import UTC, datetime
from pathlib import Path
from typing import Any


def absolute_time(value: str) -> float:
    """Parse an absolute task deadline independently of the host timezone."""
    parsed = datetime.fromisoformat(value)
    if parsed.tzinfo is None:
        raise ValueError("The deadline requires a UTC offset")
    return parsed.timestamp()


def database_path(cwd: Path) -> Path:
    """Locate hook bindings shared by this repository's linked worktrees."""
    result = subprocess.run(
        ["git", "-C", str(cwd), "rev-parse", "--git-common-dir"],
        text=True, capture_output=True, check=True,
    )
    return (cwd / result.stdout.strip()).resolve() / "foretoken-rsi.sqlite3"


class Continuation:
    """Own session bindings and consume each explicit continuation in one transaction."""

    def __init__(self, connection: sqlite3.Connection):
        self.db = connection
        self.db.row_factory = sqlite3.Row

    def create(self) -> None:
        """Create the task/member binding store when RSI is explicitly activated."""
        self.db.executescript("""
            CREATE TABLE IF NOT EXISTS sessions (
                harness TEXT NOT NULL,
                session TEXT NOT NULL,
                task TEXT NOT NULL,
                deadline REAL,
                state TEXT NOT NULL,
                reason TEXT,
                review_pending INTEGER NOT NULL DEFAULT 0,
                PRIMARY KEY (harness, session)
            );
            CREATE TABLE IF NOT EXISTS members (
                harness TEXT NOT NULL,
                session TEXT NOT NULL,
                agent TEXT NOT NULL,
                role TEXT NOT NULL,
                state TEXT NOT NULL,
                next_step TEXT,
                reason TEXT,
                PRIMARY KEY (harness, session, agent),
                FOREIGN KEY (harness, session) REFERENCES sessions(harness, session)
            );
        """)

    def session(self, harness: str, session: str) -> sqlite3.Row:
        """Require the exact activated harness/session binding for an agent command."""
        result = self.db.execute(
            "SELECT * FROM sessions WHERE harness=? AND session=?", (harness, session)
        ).fetchone()
        if result is None:
            raise ValueError("This RSI session has not been activated")
        return result

    def activate(self, args: argparse.Namespace) -> None:
        """Bind or resume one task without extending its original deadline."""
        task = args.task.resolve(strict=True)
        if not task.is_file():
            raise ValueError("The task must be a file")
        existing = self.db.execute(
            "SELECT * FROM sessions WHERE harness=? AND session=?", (args.harness, args.session)
        ).fetchone()
        if existing is not None:
            if existing["task"] != str(task):
                raise ValueError("This session is already bound to a different task")
            if existing["state"] == "complete":
                raise ValueError("A completed RSI session cannot be resumed")
            if args.deadline is not None and args.deadline != existing["deadline"]:
                raise ValueError("Resuming must preserve the original deadline")
            deadline = existing["deadline"]
            if deadline is not None and deadline <= datetime.now(UTC).timestamp():
                raise ValueError("The original task deadline has passed")
            self.db.execute(
                "UPDATE sessions SET state='active',reason=NULL WHERE harness=? AND session=?", (args.harness, args.session)
            )
            return
        if args.deadline is not None and args.deadline <= datetime.now(UTC).timestamp():
            raise ValueError("The task deadline has passed")
        self.db.execute(
            "INSERT INTO sessions(harness,session,task,deadline,state) VALUES (?,?,?,?,'active')",
            (args.harness, args.session, str(task), args.deadline),
        )

    def queue(self, args: argparse.Namespace) -> None:
        """Queue later work for a running member without dispatching its current assignment."""
        task = self.session(args.harness, args.session)
        if not args.next_step.strip():
            raise ValueError("The next step must describe concrete authorized work")
        if task["state"] != "active":
            raise ValueError("Resume the RSI session before queueing work")
        if task["deadline"] is not None and task["deadline"] <= datetime.now(UTC).timestamp():
            raise ValueError("The task deadline has passed")
        self.db.execute(
            """INSERT INTO members(harness,session,agent,role,state,next_step)
               VALUES (?,?,?,?,'working',?)
               ON CONFLICT(harness,session,agent) DO UPDATE SET
                   role=excluded.role,state='working',next_step=excluded.next_step,reason=NULL""",
            (args.harness, args.session, args.agent, args.role, args.next_step),
        )

    def release(self, args: argparse.Namespace) -> None:
        """Release a member or deactivate a task before pause, stop or completion."""
        task = self.session(args.harness, args.session)
        if not args.reason.strip():
            raise ValueError("Release requires an outcome or dependency and resume condition")
        if task["state"] == "complete":
            if not args.agent and args.state == "complete":
                return
            raise ValueError("A completed RSI session cannot be changed")
        if args.agent:
            if args.state not in {"waiting", "done"}:
                raise ValueError("A member can be released as waiting or done")
            result = self.db.execute(
                """UPDATE members SET state=?,next_step=NULL,reason=?
                   WHERE harness=? AND session=? AND agent=?""",
                (args.state, args.reason, args.harness, args.session, args.agent),
            )
            if not result.rowcount:
                raise ValueError("The member is not registered for this RSI session")
            self.db.execute(
                "UPDATE sessions SET review_pending=1 WHERE harness=? AND session=?",
                (args.harness, args.session),
            )
        else:
            if args.state not in {"paused", "complete"}:
                raise ValueError("A task can be released as paused or complete")
            self.db.execute(
                "UPDATE sessions SET state=?,reason=?,review_pending=0 WHERE harness=? AND session=?",
                (args.state, args.reason, args.harness, args.session),
            )
            self.db.execute(
                "UPDATE members SET next_step=NULL WHERE harness=? AND session=?",
                (args.harness, args.session),
            )

    def status(self, harness: str, session: str) -> dict[str, Any]:
        """Return the last continuation decisions, not a mirror of native agent activity."""
        task = dict(self.session(harness, session))
        task["members"] = [dict(row) for row in self.db.execute(
            "SELECT agent,role,state,next_step,reason FROM members WHERE harness=? AND session=? ORDER BY role,agent",
            (harness, session),
        )]
        return task

    def review(self, task: sqlite3.Row, event_name: str) -> dict[str, Any]:
        """Deliver pending completion decisions once to the lead without replacing tool results."""
        identity = (task["harness"], task["session"])
        self.db.execute(
            "UPDATE sessions SET review_pending=0 WHERE harness=? AND session=?", identity
        )
        members = self.db.execute(
            "SELECT agent,role,state,next_step FROM members WHERE harness=? AND session=? AND state!='working'",
            identity,
        ).fetchall()
        detail = "; ".join(f"{row['agent']} ({row['role']}): {row['state']}" +
                           (f"; queued work: {row['next_step']}" if row['next_step'] else "") for row in members)
        message = (
            f"Process the new RSI member delivery or dependency for {task['task']}. "
            "Read the native completion notice and its evidence. For an unfinished responsibility, "
            "give the owner its next substantive question through the native follow-up tool, "
            "or record the actual missing result and resume condition. "
            "Keep exploration assigned when a member moves into implementation or review. "
            "Record an Observer's actual completion, findings, action owners and next check time; "
            "update the existing report when results or decisions change. "
            "Use native activity for role counts; bindings describe previous decisions, not current activity."
        )
        if detail:
            message += f" Last recorded member decisions: {detail}."
        if event_name == "PostToolUse":
            return {"hookSpecificOutput": {"hookEventName": event_name, "additionalContext": message}}
        return {"decision": "block", "reason": message}

    def hook(self, harness: str, event: dict[str, Any]) -> dict[str, Any]:
        """Continue pending work once, or leave ordinary chats and released tasks untouched."""
        session = event.get("session_id")
        task = self.db.execute(
            "SELECT * FROM sessions WHERE harness=? AND session=?", (harness, session)
        ).fetchone()
        if task is None or task["state"] != "active":
            return {}
        identity = (harness, session)
        kind = event.get("hook_event_name")
        if kind in {"Interrupt", "SessionEnd"} or (
            task["deadline"] is not None and task["deadline"] <= datetime.now(UTC).timestamp()
        ):
            self.db.execute(
                "UPDATE sessions SET state=?,reason=?,review_pending=0 WHERE harness=? AND session=?",
                ("paused" if kind in {"Interrupt", "SessionEnd"} else "expired", kind, *identity),
            )
            self.db.execute(
                "UPDATE members SET next_step=NULL WHERE harness=? AND session=?", identity
            )
            return {}
        if kind in {"SubagentStop", "TeammateIdle"}:
            agent = event.get("agent_id")
            member = self.db.execute(
                "SELECT * FROM members WHERE harness=? AND session=? AND agent=?", (*identity, agent)
            ).fetchone()
            if member is None and event.get("teammate_name"):
                member = self.db.execute(
                    "SELECT * FROM members WHERE harness=? AND session=? AND agent=?",
                    (*identity, event["teammate_name"]),
                ).fetchone()
            if member is not None and member["state"] == "working" and member["next_step"] and not event.get("stop_hook_active"):
                agent = member["agent"]
                self.db.execute(
                    "UPDATE members SET next_step=NULL WHERE harness=? AND session=? AND agent=?",
                    (*identity, agent),
                )
                return {"decision": "block", "reason": (
                    f"Continue your assigned RSI {member['role']} work for {task['task']}: "
                    f"{member['next_step']}\nBinding: {harness}/{session}/{agent}. "
                    "Use the existing experiment records for evidence. "
                    "If this step genuinely depends on unavailable results, release your binding as waiting "
                    "with the missing evidence and resume condition."
                )}
            if member is not None and member["state"] == "working":
                self.db.execute(
                    "UPDATE members SET state='needs_assignment' WHERE harness=? AND session=? AND agent=?",
                    (*identity, member["agent"]),
                )
            # Native follow-up can resume a waiting member without changing its last binding decision.
            # Unregistered Observers also deliver results, but never receive queued continuation.
            self.db.execute(
                "UPDATE sessions SET review_pending=1 WHERE harness=? AND session=?", identity
            )
        elif kind in {"PostToolUse", "Stop"} and task["review_pending"] and not event.get("agent_id"):
            if kind == "PostToolUse" or not event.get("stop_hook_active"):
                return self.review(task, kind)
        return {}


def nonempty(value: str) -> str:
    """Require an explicit native identity or continuation at the CLI boundary."""
    if not value.strip():
        raise argparse.ArgumentTypeError("Expected a nonempty value")
    return value


def main() -> None:
    """Manage explicit RSI bindings and adapt native stop events without spawning agents."""
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="command", required=True)
    for name in ("activate", "queue", "release", "status"):
        command = commands.add_parser(name)
        command.add_argument("--harness", choices=("codex", "claude"), required=True)
        command.add_argument("--session", type=nonempty, required=name != "activate", help="Native root session ID; activation may use the harness session environment")
        if name == "activate":
            command.add_argument("--task", type=Path, required=True)
            command.add_argument("--deadline", type=absolute_time)
        elif name == "queue":
            command.add_argument("--agent", type=nonempty, required=True, help="Native member ID")
            command.add_argument("--role", choices=("explorer", "worker", "experimenter"), required=True)
            command.add_argument("--next-step", required=True, help="Concrete work authorized after this stage")
        elif name == "release":
            command.add_argument("--agent", type=nonempty)
            command.add_argument("--state", choices=("waiting", "done", "paused", "complete"), required=True)
            command.add_argument("--reason", required=True, help="Outcome or dependency and resume condition")
    hook = commands.add_parser("hook", help="Read a native hook event from stdin")
    hook.add_argument("--harness", choices=("codex", "claude"), required=True)
    args = parser.parse_args()
    try:
        if args.command == "activate" and not args.session:
            variable = "CODEX_THREAD_ID" if args.harness == "codex" else "CLAUDE_CODE_SESSION_ID"
            args.session = os.environ.get(variable)
            if not args.session or not args.session.strip():
                raise ValueError(f"Provide --session or set {variable} in the root session")
        event = json.load(sys.stdin) if args.command == "hook" else None
        if event is not None and not isinstance(event, dict):
            raise ValueError("A hook event must be a JSON object")
        cwd = Path(event.get("cwd", Path.cwd())) if event is not None else Path.cwd()
        try:
            path = database_path(cwd)
        except subprocess.CalledProcessError:
            if event is None:
                raise ValueError("RSI bindings require a Git checkout") from None
            print("{}")
            return
        if args.command != "activate" and not path.exists():
            if event is not None:
                print("{}")
                return
            raise ValueError("No RSI session has been activated in this repository")
        with closing(sqlite3.connect(path)) as connection:
            connection.execute("PRAGMA foreign_keys=ON")
            state = Continuation(connection)
            if args.command == "activate":
                state.create()
            connection.execute("BEGIN IMMEDIATE")
            with connection:
                if event is not None:
                    result = state.hook(args.harness, event)
                elif args.command == "status":
                    result = state.status(args.harness, args.session)
                else:
                    getattr(state, args.command)(args)
                    result = state.status(args.harness, args.session)
        if event is not None and event.get("hook_event_name") == "TeammateIdle" and result:
            print(result["reason"], file=sys.stderr)
            raise SystemExit(2)
        print(json.dumps(result, ensure_ascii=False, indent=2))
    except (OSError, ValueError, sqlite3.Error) as error:
        parser.exit(1, f"{error}\n")


if __name__ == "__main__":
    main()
