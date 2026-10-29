# SPDX-License-Identifier: Apache-2.0
# SPDX-FileCopyrightText: Copyright contributors to the Foretoken project

"""Focused state-machine checks for the RSI continuation pool."""

from __future__ import annotations

import importlib.util
import sqlite3
import time
import unittest
from argparse import Namespace
from pathlib import Path


def load_module(name: str) -> object:
    """Load a sibling script without making the skills directory a package."""
    path = Path(__file__).with_name(f"{name}.py")
    spec = importlib.util.spec_from_file_location(name, path)
    if spec is None or spec.loader is None:
        raise ImportError(f"Cannot load {path}")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


Continuation = load_module("continuation").Continuation
WorkPool = load_module("work_pool").WorkPool


class WorkPoolTest(unittest.TestCase):
    """Protect ownership, review independence and fallback scheduling contracts."""

    def setUp(self) -> None:
        self.db = sqlite3.connect(":memory:")
        self.db.row_factory = sqlite3.Row
        self.state = Continuation(self.db)
        self.state.create()
        self.db.execute(
            "INSERT INTO sessions(harness,session,task,deadline,started,state) VALUES (?,?,?,?,?,'active')",
            ("codex", "root", "/tmp/task.md", None, time.time() - 7200),
        )
        WorkPool.create(self.db)
        self.pool = WorkPool(self.db, "codex", "root")

    def tearDown(self) -> None:
        self.db.close()

    def test_explicit_exclusion_blocks_an_explorer_review(self) -> None:
        """A contributor outside execution roles can be excluded explicitly from review."""
        self.pool.register("explorer-01", "explorer", [])
        self.pool.submit("impl", "explorer", "implement", "explorer-01")
        self.assertEqual(self.pool.claim("explorer-01")["id"], "impl")
        self.pool.finish("impl", "explorer-01", Path(__file__))
        self.pool.submit("review", "review", "review", "root", after=["impl"], exclude=["explorer-01"])
        self.assertFalse(self.pool.eligible(self.pool.ready()[0], "explorer-01", {"review"}))

    def test_handoff_clears_owner_and_retains_previous_owner(self) -> None:
        """A recovered item is claimable by a peer while preserving its handoff audit."""
        self.pool.register("worker-01", "worker", [])
        self.pool.submit("candidate", "worker", "candidate", "worker-01")
        self.pool.claim("worker-01")
        self.pool.handoff("candidate", "worker-01", "job 123 stopped")
        row = self.pool.item("candidate")
        self.assertIsNone(row["owner"])
        self.assertEqual(row["previous_owner"], "worker-01")

    def test_root_fallback_materializes_due_evaluator(self) -> None:
        """A root Stop event advances the schedule even when no member-idle event arrives."""
        self.pool.schedule(interval=60, count=1)
        result = self.state.hook("codex", {"session_id": "root", "hook_event_name": "Stop"})
        self.assertEqual(result, {})
        self.assertEqual(self.db.execute("SELECT COUNT(*) FROM work WHERE kind='evaluation'").fetchone()[0], 1)

    def test_session_end_does_not_create_new_evaluator_work(self) -> None:
        """SessionEnd is advisory and cannot create work after the root is ending."""
        self.pool.schedule(interval=60, count=1)
        self.assertEqual(self.state.hook("codex", {"session_id": "root", "hook_event_name": "SessionEnd"}), {})
        self.assertEqual(self.db.execute("SELECT COUNT(*) FROM work WHERE creator='evaluation_schedule'").fetchone()[0], 0)

    def test_manual_evaluation_does_not_block_scheduled_batch(self) -> None:
        """A member's review evaluation does not suppress the anchored fresh Evaluator."""
        self.pool.schedule(interval=60, count=1)
        self.pool.register("worker-01", "worker", ["evaluation"])
        self.pool.submit("manual-eval", "evaluation", "manual", "worker-01")
        self.state.hook("codex", {"session_id": "root", "hook_event_name": "Stop"})
        self.assertEqual(self.db.execute(
            "SELECT COUNT(*) FROM work WHERE kind='evaluation' AND creator='evaluation_schedule'"
        ).fetchone()[0], 1)
        self.assertEqual(self.pool.item("manual-eval")["state"], "ready")

    def test_rescheduled_due_batch_avoids_cancelled_id(self) -> None:
        """Re-enabling checks after count zero keeps the cancelled batch auditable."""
        self.pool.schedule(interval=60, count=1)
        self.pool.due_evaluations(self.state.session("codex", "root"))
        self.pool.schedule(interval=60, count=0)
        self.pool.schedule(interval=60, count=1)
        self.db.execute("UPDATE evaluation_schedule SET next_due=?", (time.time() - 1,))
        self.pool.due_evaluations(self.state.session("codex", "root"))
        ids = [row[0] for row in self.db.execute("SELECT id FROM work WHERE creator='evaluation_schedule'")]
        self.assertEqual(len(ids), 2)
        self.assertNotEqual(ids[0], ids[1])

    def test_guarded_member_stop_leaves_a_root_notice(self) -> None:
        """A native stop guard records unfinished ownership for the next root event."""
        self.pool.register("worker-01", "worker", [])
        self.pool.submit("candidate", "worker", "candidate", "worker-01")
        self.pool.claim("worker-01")
        self.assertEqual(self.state.hook("codex", {
            "session_id": "root", "hook_event_name": "SubagentStop", "agent_id": "worker-01",
            "stop_hook_active": True,
        }), {})
        notice = self.state.hook("codex", {"session_id": "root", "hook_event_name": "PostToolUse"})
        self.assertEqual(notice["hookSpecificOutput"]["hookEventName"], "PostToolUse")
        self.assertIn("candidate", notice["hookSpecificOutput"]["additionalContext"])
        self.assertEqual(self.db.execute("SELECT review_pending FROM sessions").fetchone()[0], 0)

    def test_hook_falls_back_to_known_teammate_name(self) -> None:
        """A harness wrapper ID cannot hide a registered teammate identity."""
        self.pool.register("worker-01", "worker", [])
        self.pool.submit("candidate", "worker", "candidate", "worker-01")
        result = self.state.hook("codex", {
            "session_id": "root", "hook_event_name": "SubagentStop", "agent_id": "opaque-id",
            "teammate_name": "worker-01",
        })
        self.assertEqual(result["decision"], "block")
        self.assertIn("worker-01", result["reason"])

    def test_queue_preserves_active_and_handed_off_work(self) -> None:
        """A new legacy step gets a new ID and cannot overwrite recovery work."""
        self.pool.register("worker-01", "worker", [])
        self.pool.submit("next:worker-01:1", "worker", "live", "worker-01")
        self.pool.claim("worker-01")
        work = self.state.queue(Namespace(
            harness="codex", session="root", agent="worker-01", role="worker", next_step="resume",
        ))
        self.assertEqual(work, "next:worker-01:2")
        self.pool.handoff("next:worker-01:1", "worker-01", "recover it")
        preserved = self.state.queue(Namespace(
            harness="codex", session="root", agent="worker-01", role="worker", next_step="another",
        ))
        self.assertEqual(preserved, "next:worker-01:2")
        self.assertEqual(self.pool.item("next:worker-01:1")["description"], "live")

    def test_all_roles_receive_continuing_work(self) -> None:
        """An empty queue directs every continuing role to a substantive next question."""
        for agent, role in (("explorer-01", "explorer"), ("worker-01", "worker"), ("experimenter-01", "experimenter")):
            self.pool.register(agent, role, [])
            action = self.pool.idle_action(agent)
            self.assertEqual(action["action"], "research")
            self.assertIn(role, action["instruction"])

    def test_released_member_cannot_claim_until_resumed(self) -> None:
        """Waiting and done releases require native re-registration before new claims."""
        self.pool.register("worker-01", "worker", [])
        self.pool.submit("candidate", "worker", "candidate", "root")
        self.db.execute("UPDATE members SET state='waiting' WHERE agent='worker-01'")
        with self.assertRaisesRegex(ValueError, "Register or resume"):
            self.pool.claim("worker-01")


if __name__ == "__main__":
    unittest.main()
