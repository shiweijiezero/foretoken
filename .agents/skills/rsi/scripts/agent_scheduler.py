# SPDX-License-Identifier: Apache-2.0
# SPDX-FileCopyrightText: Copyright contributors to the Foretoken project

"""Wake persisted RSI member responsibilities through the Codex app-server."""

from __future__ import annotations

import argparse
import importlib.util
import json
import os
import selectors
import sqlite3
import subprocess
import time
import uuid
from contextlib import closing
from pathlib import Path
from typing import Any

_continuation_spec = importlib.util.spec_from_file_location(
    "rsi_continuation", Path(__file__).with_name("continuation.py")
)
if _continuation_spec is None or _continuation_spec.loader is None:
    raise ImportError("Cannot load continuation.py")
_continuation = importlib.util.module_from_spec(_continuation_spec)
_continuation_spec.loader.exec_module(_continuation)
Continuation = _continuation.Continuation
WorkPool = _continuation.WorkPool
database_path = _continuation.database_path


class AppServerClient:
    """Drive one app-server JSON-RPC connection and ignore interleaved notifications."""

    def __init__(self, socket_path: str | None, *, command: str = "codex"):
        argv = [command, "app-server"]
        if socket_path:
            argv.extend(("proxy", "--sock", socket_path))
        self.process = subprocess.Popen(
            argv,
            stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
            text=True, bufsize=1,
        )
        self._selector = selectors.DefaultSelector()
        if self.process.stdout is None or self.process.stdin is None:
            raise OSError("app-server proxy did not provide stdio")
        self._selector.register(self.process.stdout, selectors.EVENT_READ)
        self._next_id = 1
        self._request("initialize", {"clientInfo": {
            "name": "foretoken-rsi-agent-scheduler",
            "title": "Foretoken RSI Agent Scheduler",
            "version": "0.1.0",
        }})
        self._send({"method": "initialized", "params": {}})

    def _send(self, message: dict[str, Any]) -> None:
        if self.process.stdin is None:
            raise OSError("app-server proxy stdin is closed")
        self.process.stdin.write(json.dumps(message, ensure_ascii=False) + "\n")
        self.process.stdin.flush()

    def _request(self, method: str, params: dict[str, Any]) -> dict[str, Any]:
        request_id = self._next_id
        self._next_id += 1
        self._send({"id": request_id, "method": method, "params": params})
        deadline = time.monotonic() + 30
        while time.monotonic() < deadline:
            events = self._selector.select(max(0, deadline - time.monotonic()))
            for key, _ in events:
                line = key.fileobj.readline()
                if not line:
                    raise OSError("app-server proxy closed its output")
                message = json.loads(line)
                if message.get("id") != request_id:
                    continue
                if "error" in message:
                    raise RuntimeError(f"{method} failed: {message['error']}")
                return message.get("result", {})
        raise TimeoutError(f"Timed out waiting for app-server {method}")

    def thread_status(self, thread_id: str) -> str:
        """Read the authoritative runtime status without starting a turn."""
        result = self._request("thread/read", {"threadId": thread_id, "includeTurns": False})
        return (result.get("thread", {}).get("status") or {}).get("type", "unknown")

    def resume(self, thread_id: str) -> None:
        """Load the existing thread before appending a scheduled responsibility."""
        self._request("thread/resume", {"threadId": thread_id, "excludeTurns": True})

    def start(self, thread_id: str, prompt: str, cwd: str) -> str:
        """Start one turn and return its server-assigned turn ID."""
        result = self._request("turn/start", {
            "threadId": thread_id,
            "input": [{"type": "text", "text": prompt}],
            "cwd": cwd,
        })
        turn_id = result.get("turn", {}).get("id")
        if not turn_id:
            raise RuntimeError("turn/start returned no turn id")
        return turn_id

    def close(self) -> None:
        """Close the scheduler's client connection without interrupting member turns."""
        self._selector.close()
        self.process.terminate()
        try:
            self.process.wait(timeout=2)
        except subprocess.TimeoutExpired:
            self.process.kill()


def run_once(connection: Any, state: Any, client: AppServerClient, harness: str,
             owner: str, cwd: Path, lease_seconds: float, session: str | None = None) -> int:
    """Reconcile native statuses and start each newly leased responsibility once."""
    actions = 0
    for member in state.supervisor_candidates(harness, session):
        try:
            status = client.thread_status(member["thread_id"])
        except (OSError, RuntimeError, TimeoutError) as error:
            status = "systemError"
            connection.execute("""UPDATE members SET last_error=?
                WHERE harness=? AND session=? AND agent=?""",
                               (str(error)[:2000], harness, member["session"], member["agent"]))
            connection.commit()
        connection.execute("BEGIN IMMEDIATE")
        with connection:
            action = state.supervisor_prepare(
                harness, member["session"], member["agent"], owner, status, lease_seconds,
            )
        if action is None:
            continue
        actions += 1
        try:
            client.resume(action["thread_id"])
            turn_id = client.start(action["thread_id"], action["prompt"], str(cwd))
        except (OSError, RuntimeError, TimeoutError) as error:
            connection.execute("BEGIN IMMEDIATE")
            with connection:
                state.supervisor_failed(
                    harness, member["session"], member["agent"], owner,
                    action["dispatch_id"], str(error),
                )
            continue
        connection.execute("BEGIN IMMEDIATE")
        with connection:
            state.supervisor_started(
                harness, member["session"], member["agent"], owner,
                action["dispatch_id"], turn_id,
            )
    return actions


def main() -> None:
    """Run an independent, lease-backed scheduler until the task is explicitly stopped."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--harness", choices=("codex", "claude"), default="codex")
    parser.add_argument("--session", required=True, help="One activated RSI root session to keep alive")
    parser.add_argument("--socket", default=os.environ.get("CODEX_APP_SERVER_SOCKET"),
                        help="Existing app-server Unix socket; omit to start a stdio app-server")
    parser.add_argument("--cwd", type=Path, default=Path.cwd())
    parser.add_argument("--interval", type=float, default=2.0)
    parser.add_argument("--lease", type=float, default=30.0)
    parser.add_argument("--once", action="store_true")
    parser.add_argument("--owner", default=f"scheduler-{uuid.uuid4().hex}")
    args = parser.parse_args()
    if args.interval <= 0 or args.lease <= 0:
        parser.error("interval and lease must be positive")
    path = database_path(args.cwd)
    with closing(sqlite3.connect(path)) as connection:
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA foreign_keys=ON")
        state = Continuation(connection)
        state.create()
        WorkPool.create(connection)
        connection.commit()
        client = AppServerClient(args.socket)
        try:
            while True:
                active = connection.execute(
                    "SELECT state,deadline FROM sessions WHERE harness=? AND session=?",
                    (args.harness, args.session),
                ).fetchone()
                if active is None or active[0] != "active":
                    break
                if active[1] is not None and active[1] <= time.time():
                    connection.execute("""UPDATE sessions SET state='expired',reason='deadline',review_pending=0
                                       WHERE harness=? AND session=? AND state='active'""",
                                       (args.harness, args.session))
                    connection.commit()
                    break
                run_once(connection, state, client, args.harness, args.owner, args.cwd, args.lease, args.session)
                if args.once:
                    break
                time.sleep(args.interval)
        finally:
            client.close()


if __name__ == "__main__":
    main()
