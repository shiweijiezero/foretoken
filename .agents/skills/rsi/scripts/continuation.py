# SPDX-License-Identifier: Apache-2.0
# SPDX-FileCopyrightText: Copyright contributors to the Foretoken project

"""Coordinate autonomous RSI members through shared work and native continuation hooks."""

from __future__ import annotations

import argparse
import importlib.util
import json
import os
import sqlite3
import subprocess
import sys
import time
from contextlib import closing
from datetime import UTC, datetime
from pathlib import Path
from typing import Any


_pool_spec = importlib.util.spec_from_file_location("rsi_work_pool", Path(__file__).with_name("work_pool.py"))
_pool_module = importlib.util.module_from_spec(_pool_spec)
_pool_spec.loader.exec_module(_pool_module)
WorkPool = _pool_module.WorkPool
KINDS = _pool_module.KINDS


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
    """Own task lifecycle and keep native member continuation local to its owner."""

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
                started REAL,
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
        if "started" not in {row[1] for row in self.db.execute("PRAGMA table_info(sessions)")}:
            self.db.execute("ALTER TABLE sessions ADD COLUMN started REAL")

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
            if args.started_at is not None:
                if existing["started"] is not None and args.started_at != existing["started"]:
                    raise ValueError("Resuming must preserve the original task start")
                self.db.execute("UPDATE sessions SET started=? WHERE harness=? AND session=?", (args.started_at, args.harness, args.session))
            deadline = existing["deadline"]
            if deadline is not None and deadline <= datetime.now(UTC).timestamp():
                raise ValueError("The original task deadline has passed")
            self.db.execute(
                "UPDATE sessions SET state='active',reason=NULL,review_pending=0 WHERE harness=? AND session=?", (args.harness, args.session)
            )
            queued = self.db.execute(
                "SELECT agent,role,next_step FROM members WHERE harness=? AND session=? AND next_step IS NOT NULL",
                (args.harness, args.session),
            ).fetchall()
            for member in queued:
                self.queue(argparse.Namespace(
                    harness=args.harness, session=args.session, agent=member["agent"],
                    role=member["role"], next_step=member["next_step"],
                ))
            self.db.execute("UPDATE members SET next_step=NULL WHERE harness=? AND session=?", (args.harness, args.session))
            return
        if args.deadline is not None and args.deadline <= datetime.now(UTC).timestamp():
            raise ValueError("The task deadline has passed")
        self.db.execute(
            "INSERT INTO sessions(harness,session,task,deadline,started,state) VALUES (?,?,?,?,?,'active')",
            (args.harness, args.session, str(task), args.deadline, args.started_at if args.started_at is not None else time.time()),
        )

    def queue(self, args: argparse.Namespace) -> str:
        """Replace a member's unclaimed legacy continuation without changing active or recovery work."""
        pool = WorkPool(self.db, args.harness, args.session)
        accepts = [row[0] for row in self.db.execute(
            "SELECT kind FROM capabilities WHERE harness=? AND session=? AND agent=?",
            (args.harness, args.session, args.agent),
        )]
        pool.register(args.agent, args.role, accepts)
        prefix = f"next:{args.agent}:"
        pending = self.db.execute("""SELECT id FROM work WHERE harness=? AND session=? AND creator=?
            AND substr(id,1,?)=? AND state='ready' AND owner IS NULL ORDER BY created,id""",
            (args.harness, args.session, args.agent, len(prefix), prefix)).fetchall()
        if pending:
            work = pending[0]["id"]
            self.db.execute("UPDATE work SET kind=?,description=? WHERE harness=? AND session=? AND id=?",
                            (args.role, args.next_step, args.harness, args.session, work))
            for duplicate in pending[1:]:
                self.db.execute("UPDATE work SET state='cancelled',reason='Replaced legacy next step' WHERE harness=? AND session=? AND id=?",
                                (args.harness, args.session, duplicate["id"]))
        else:
            latest = self.db.execute("""SELECT MAX(CAST(substr(id,?) AS INTEGER)) FROM work
                WHERE harness=? AND session=? AND creator=? AND substr(id,1,?)=?""",
                (len(prefix) + 1, args.harness, args.session, args.agent, len(prefix), prefix)).fetchone()[0]
            work = prefix + str((latest or 0) + 1)
            pool.submit(work, args.role, args.next_step, args.agent)
        return work

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
            active = self.db.execute(
                "SELECT id FROM work WHERE harness=? AND session=? AND owner=? AND state='active'",
                (args.harness, args.session, args.agent),
            ).fetchall()
            if active:
                raise ValueError("Finish or hand off owned work before releasing the member: " + ", ".join(row[0] for row in active))
            result = self.db.execute(
                """UPDATE members SET state=?,next_step=NULL,reason=?
                   WHERE harness=? AND session=? AND agent=?""",
                (args.state, args.reason, args.harness, args.session, args.agent),
            )
            if not result.rowcount:
                raise ValueError("The member is not registered for this RSI session")
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
        """Return this task's recorded continuation decisions and queued steps."""
        task = dict(self.session(harness, session))
        task.pop("review_pending", None)
        task["members"] = [dict(row) for row in self.db.execute(
            "SELECT agent,role,state,next_step,reason FROM members WHERE harness=? AND session=? ORDER BY role,agent",
            (harness, session),
        )]
        task["work"] = WorkPool(self.db, harness, session).status()
        return task

    def hook(self, harness: str, event: dict[str, Any]) -> dict[str, Any]:
        """Return an unfinished member to its own work; ordinary root events require no decisions."""
        session = event.get("session_id")
        task = self.db.execute(
            "SELECT * FROM sessions WHERE harness=? AND session=?", (harness, session)
        ).fetchone()
        if task is None or task["state"] != "active":
            return {}
        identity = (harness, session)
        kind = event.get("hook_event_name")
        if task["deadline"] is not None and task["deadline"] <= time.time():
            self.db.execute("UPDATE sessions SET state='expired',reason='deadline',review_pending=0 WHERE harness=? AND session=?", identity)
            return {}
        if kind == "Interrupt" and not event.get("agent_id"):
            self.db.execute("UPDATE sessions SET state='paused',reason='Interrupt',review_pending=0 WHERE harness=? AND session=?", identity)
            return {}
        if kind not in {"SubagentStop", "TeammateIdle"} or event.get("stop_hook_active"):
            return {}
        agent = event.get("agent_id") or event.get("teammate_name")
        member = self.db.execute("SELECT * FROM members WHERE harness=? AND session=? AND agent=?", (*identity, agent)).fetchone()
        if member is None or member["state"] in {"waiting", "done"}:
            return {}
        owned = self.db.execute("SELECT id FROM work WHERE harness=? AND session=? AND owner=? AND state='active'", (*identity, agent)).fetchall()
        if owned:
            instruction = (
                "Continue the work you own: " + ", ".join(row[0] for row in owned) + ". "
                "Finish with the original evidence or hand off the same work with its live job and recovery references. "
                "Inspect existing jobs before launching anything again."
            )
        else:
            action = WorkPool(self.db, harness, session).idle_action(agent)
            instruction = "Claim eligible ready work first. If none is ready: " + action["instruction"]
        return {"decision": "block", "reason": (
            f"Continue your RSI {member['role']} responsibility for {task['task']}. {instruction} "
            f"Binding: {harness}/{session}/{agent}. Advance within the task's budget without lead approval. "
            "Release as waiting only for a dependency that no ready work can resolve, or done when your responsibility is complete."
        )}


def nonempty(value: str) -> str:
    """Require an explicit native identity or continuation at the CLI boundary."""
    if not value.strip():
        raise argparse.ArgumentTypeError("Expected a nonempty value")
    return value


def main() -> None:
    """Run task-scoped pool operations; waiting releases SQLite between short claim transactions."""
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="command", required=True)
    for name in ("activate", "register", "submit", "claim", "finish", "handoff", "schedule", "queue", "release", "status"):
        command = commands.add_parser(name)
        command.add_argument("--harness", choices=("codex", "claude"), required=True)
        command.add_argument("--session", type=nonempty, required=name != "activate", help="Activated task/root session ID")
        if name == "activate":
            command.add_argument("--task", type=Path, required=True)
            command.add_argument("--deadline", type=absolute_time)
            command.add_argument("--started-at", type=absolute_time, help="Original task start when adopting an earlier session")
        if name in {"register", "submit", "claim", "finish", "handoff", "queue"}:
            command.add_argument("--agent", type=nonempty, required=True)
        if name in {"register", "queue"}:
            command.add_argument("--role", choices=("explorer", "worker", "experimenter"), required=True)
        if name == "register":
            command.add_argument("--accept", action="append", choices=KINDS, default=[], help="Additional authorized responsibility when dedicated staffing is absent")
        elif name == "submit":
            command.add_argument("--work", type=nonempty, required=True)
            command.add_argument("--kind", choices=KINDS, required=True)
            command.add_argument("--description", type=nonempty, required=True, help="Substantive question or stage and its original artifact paths")
            command.add_argument("--topic", default="")
            command.add_argument("--environment", default="")
            command.add_argument("--after", action="append", default=[])
            command.add_argument("--exclude", action="append", default=[])
        elif name == "claim":
            command.add_argument("--wait", type=float, default=0, help="Seconds to await ready work inside the tool, without a reasoning loop")
        elif name in {"finish", "handoff"}:
            command.add_argument("--work", type=nonempty, required=True)
            command.add_argument("--result", type=Path, required=name == "finish")
            command.add_argument("--reason", type=nonempty, required=name == "handoff")
            if name == "handoff":
                command.add_argument("--from-agent", type=nonempty, help="Expected previous owner, after confirming its native execution has stopped; include evidence and original job references in --reason")
        elif name == "schedule":
            command.add_argument("--interval", type=float, required=True, help="Seconds between fresh Evaluator batches")
            command.add_argument("--count", type=int, required=True)
        elif name == "queue":
            command.add_argument("--next-step", type=nonempty, required=True)
        elif name == "release":
            command.add_argument("--agent", type=nonempty)
            command.add_argument("--state", choices=("waiting", "done", "paused", "complete"), required=True)
            command.add_argument("--reason", type=nonempty, required=True)
    hook = commands.add_parser("hook", help="Read a native hook event from stdin")
    hook.add_argument("--harness", choices=("codex", "claude"), required=True)
    args = parser.parse_args()
    try:
        if args.command == "activate" and not args.session:
            variable = "CODEX_THREAD_ID" if args.harness == "codex" else "CLAUDE_CODE_SESSION_ID"
            args.session = os.environ.get(variable)
            if not args.session or not args.session.strip():
                raise ValueError(f"Provide --session or set {variable} in the root session")
        if args.command == "claim" and args.wait < 0:
            raise ValueError("Wait duration must be nonnegative")
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
            if event is not None:
                tables = {row[0] for row in connection.execute("SELECT name FROM sqlite_master WHERE type='table'")}
                if "sessions" not in tables:
                    print("{}")
                    return
                binding = connection.execute("SELECT state FROM sessions WHERE harness=? AND session=?",
                                             (args.harness, event.get("session_id"))).fetchone()
                if binding is None or binding[0] != "active" or not {"work", "capabilities"}.issubset(tables):
                    print("{}")
                    return
            else:
                state.create()
                WorkPool.create(connection)
            wait_until = time.monotonic() + (args.wait if args.command == "claim" else 0)
            while True:
                connection.execute("BEGIN IMMEDIATE")
                with connection:
                    pool = WorkPool(connection, args.harness, args.session) if event is None else None
                    if event is not None:
                        result = state.hook(args.harness, event)
                    elif args.command == "status":
                        result = state.status(args.harness, args.session)
                    elif args.command == "register":
                        pool.register(args.agent, args.role, args.accept)
                        result = {"agent": args.agent, "role": args.role, "accepts": [row[0] for row in connection.execute(
                            "SELECT kind FROM capabilities WHERE harness=? AND session=? AND agent=? ORDER BY kind",
                            (args.harness, args.session, args.agent))]}
                    elif args.command == "submit":
                        pool.submit(args.work, args.kind, args.description, args.agent, topic=args.topic,
                                    environment=args.environment, after=args.after, exclude=args.exclude)
                        result = pool.item(args.work)
                    elif args.command == "claim":
                        result = pool.claim(args.agent)
                        if result is None:
                            action = pool.idle_action(args.agent)
                            if action["action"] == "research" or time.monotonic() >= wait_until:
                                result = action
                    elif args.command == "finish":
                        pool.finish(args.work, args.agent, args.result)
                        result = pool.item(args.work)
                    elif args.command == "handoff":
                        pool.handoff(args.work, args.agent, args.reason, args.from_agent)
                        result = pool.item(args.work)
                    elif args.command == "schedule":
                        pool.schedule(args.interval, args.count)
                        result = dict(connection.execute("SELECT interval,count,next_due FROM evaluation_schedule WHERE harness=? AND session=?",
                                                         (args.harness, args.session)).fetchone())
                    elif args.command == "queue":
                        result = pool.item(state.queue(args))
                    else:
                        getattr(state, args.command)(args)
                        if args.command == "release" and args.agent:
                            result = dict(connection.execute("SELECT agent,role,state,reason FROM members WHERE harness=? AND session=? AND agent=?",
                                                             (args.harness, args.session, args.agent)).fetchone())
                        else:
                            result = dict(state.session(args.harness, args.session))
                            result.pop("review_pending", None)
                if args.command != "claim" or result is not None or time.monotonic() >= wait_until:
                    break
                time.sleep(min(0.25, max(0, wait_until - time.monotonic())))
        if event is not None and event.get("hook_event_name") == "TeammateIdle" and result:
            print(result["reason"], file=sys.stderr)
            raise SystemExit(2)
        print(json.dumps(result, ensure_ascii=False, indent=2))
    except (OSError, ValueError, sqlite3.Error) as error:
        parser.exit(1, f"{error}\n")


if __name__ == "__main__":
    main()
