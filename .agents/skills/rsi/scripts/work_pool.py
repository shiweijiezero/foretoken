# SPDX-License-Identifier: Apache-2.0
# SPDX-FileCopyrightText: Copyright contributors to the Foretoken project

"""Share substantive RSI work between native members without a dispatching agent."""

from __future__ import annotations

import sqlite3
import time
from pathlib import Path
from typing import Any

KINDS = ("explorer", "worker", "experimenter", "review", "evaluation")
CAPABILITIES = {
    "explorer": ("explorer", "review", "evaluation"),
    "worker": ("worker", "review", "evaluation"),
    "experimenter": ("experimenter", "review", "evaluation"),
}


class WorkPool:
    """Own task-local readiness and atomic claims; experiment artifacts stay with their producers."""

    def __init__(self, db: sqlite3.Connection, harness: str, session: str):
        self.db, self.identity = db, (harness, session)

    @staticmethod
    def create(db: sqlite3.Connection) -> None:
        """Add scheduling records alongside existing RSI task bindings."""
        db.executescript("""
            CREATE TABLE IF NOT EXISTS capabilities (
                harness TEXT, session TEXT, agent TEXT, kind TEXT,
                PRIMARY KEY(harness,session,agent,kind)
            );
            CREATE TABLE IF NOT EXISTS work (
                harness TEXT, session TEXT, id TEXT, kind TEXT NOT NULL,
                description TEXT NOT NULL, creator TEXT NOT NULL,
                topic TEXT NOT NULL, environment TEXT NOT NULL,
                state TEXT NOT NULL DEFAULT 'ready', owner TEXT,
                previous_owner TEXT,
                result TEXT, reason TEXT, created REAL NOT NULL,
                PRIMARY KEY(harness,session,id),
                FOREIGN KEY(harness,session) REFERENCES sessions(harness,session)
            );
            CREATE TABLE IF NOT EXISTS work_participants (
                harness TEXT, session TEXT, work TEXT, agent TEXT,
                PRIMARY KEY(harness,session,work,agent),
                FOREIGN KEY(harness,session,work) REFERENCES work(harness,session,id)
            );
            CREATE TABLE IF NOT EXISTS dependencies (
                harness TEXT, session TEXT, work TEXT, dependency TEXT,
                PRIMARY KEY(harness,session,work,dependency),
                FOREIGN KEY(harness,session,work) REFERENCES work(harness,session,id),
                FOREIGN KEY(harness,session,dependency) REFERENCES work(harness,session,id)
            );
            CREATE TABLE IF NOT EXISTS exclusions (
                harness TEXT, session TEXT, work TEXT, agent TEXT,
                PRIMARY KEY(harness,session,work,agent),
                FOREIGN KEY(harness,session,work) REFERENCES work(harness,session,id)
            );
            CREATE TABLE IF NOT EXISTS evaluation_schedule (
                harness TEXT, session TEXT, interval REAL NOT NULL,
                count INTEGER NOT NULL, next_due REAL NOT NULL,
                PRIMARY KEY(harness,session)
            );
        """)
        columns = {row[1] for row in db.execute("PRAGMA table_info(work)")}
        if "previous_owner" not in columns:
            try:
                db.execute("ALTER TABLE work ADD COLUMN previous_owner TEXT")
            except sqlite3.OperationalError as error:
                if "duplicate column name" not in str(error):
                    raise
        # A ready item has no current owner. Preserve the old handoff owner for
        # review independence while making legacy rows claimable after upgrade.
        db.execute("""UPDATE work SET previous_owner=COALESCE(previous_owner, owner), owner=NULL
            WHERE state='ready' AND owner IS NOT NULL""")

    def active(self) -> sqlite3.Row:
        """Require an active task before admitting work or a claim."""
        task = self.db.execute("SELECT * FROM sessions WHERE harness=? AND session=?", self.identity).fetchone()
        if task is None:
            raise ValueError("This RSI session has not been activated")
        if task["state"] != "active":
            raise ValueError(f"RSI task is {task['state']}")
        if task["deadline"] is not None and task["deadline"] <= time.time():
            raise ValueError("The task deadline has passed")
        return task

    def register(self, agent: str, role: str, accepts: list[str]) -> None:
        """Advertise this member's permitted work kinds without reserving pending work."""
        self.active()
        self.db.execute("""INSERT INTO members(harness,session,agent,role,state,next_step,notice)
            VALUES (?,?,?,?,'working',NULL,NULL) ON CONFLICT(harness,session,agent)
            DO UPDATE SET role=excluded.role,state='working',next_step=NULL,reason=NULL,notice=NULL""",
                        (*self.identity, agent, role))
        self.db.execute("DELETE FROM capabilities WHERE harness=? AND session=? AND agent=?", (*self.identity, agent))
        self.db.executemany("INSERT INTO capabilities VALUES (?,?,?,?)", [
            (*self.identity, agent, kind) for kind in dict.fromkeys((*CAPABILITIES[role], *accepts))
        ])

    def submit(self, work: str, kind: str, description: str, agent: str, *, topic: str = "",
               environment: str = "", after: list[str] = (), exclude: list[str] = ()) -> None:
        """Publish one question or executable stage, referencing its existing evidence in the description."""
        self.active()
        if work in after:
            raise ValueError("Work cannot depend on itself")
        self.db.execute("""INSERT INTO work(harness,session,id,kind,description,creator,topic,environment,created)
            VALUES (?,?,?,?,?,?,?,?,?)""", (*self.identity, work, kind, description, agent, topic, environment, time.time()))
        self.db.executemany("INSERT INTO dependencies VALUES (?,?,?,?)", [(*self.identity, work, dep) for dep in after])
        self.db.executemany("INSERT INTO exclusions VALUES (?,?,?,?)", [(*self.identity, work, who) for who in set(exclude)])

    def schedule(self, interval: float, count: int) -> None:
        """Keep a task's original evaluation anchor when enabling or resuming periodic checks."""
        task = self.active()
        if interval <= 0 or count < 0:
            raise ValueError("Evaluation interval must be positive and count must be nonnegative")
        if task["started"] is None:
            raise ValueError("Original task start is unknown; activate with --started-at before scheduling")
        if count == 0:
            self.db.execute("""UPDATE work SET state='cancelled',reason='Evaluator count is zero'
                WHERE harness=? AND session=? AND kind='evaluation' AND creator='evaluation_schedule' AND state='ready'""", self.identity)
        self.db.execute("""INSERT INTO evaluation_schedule VALUES (?,?,?,?,?)
            ON CONFLICT(harness,session) DO UPDATE SET interval=excluded.interval,count=excluded.count""",
            (*self.identity, interval, count, task["started"] + interval))

    def due_evaluations(self, task: sqlite3.Row) -> None:
        """Make one due evaluation batch claimable, without accumulating missed rounds."""
        schedule = self.db.execute("SELECT * FROM evaluation_schedule WHERE harness=? AND session=?", self.identity).fetchone()
        if schedule is None or not schedule["count"] or schedule["next_due"] > time.time():
            return
        pending = self.db.execute("""SELECT 1 FROM work WHERE harness=? AND session=?
            AND kind='evaluation' AND creator='evaluation_schedule' AND state IN ('ready','active') LIMIT 1""", self.identity).fetchone()
        if pending:
            return
        due = schedule["next_due"]
        for index in range(schedule["count"]):
            work = f"evaluation:{due}:{index}"
            retry = 0
            while self.db.execute("SELECT 1 FROM work WHERE harness=? AND session=? AND id=?", (*self.identity, work)).fetchone():
                retry += 1
                work = f"evaluation:{due}:{index}:retry{retry}"
            self.submit(work, "evaluation", (
                f"Create one fresh Evaluator without inherited history for {task['task']}. "
                "Give only the task, Evaluator role and working directory. Deliver findings directly to their owners; "
                "finish this item with the completed assessment's path, not the child's dispatch notice."
            ), "evaluation_schedule")
        elapsed = int((time.time() - due) // schedule["interval"]) + 1
        self.db.execute("UPDATE evaluation_schedule SET next_due=? WHERE harness=? AND session=?",
                        (due + elapsed * schedule["interval"], *self.identity))

    def claim(self, agent: str) -> dict[str, Any] | None:
        """Claim ready work atomically, preferring familiar topics and environments over FIFO peers."""
        task = self.active()
        self.due_evaluations(task)
        capable = {row[0] for row in self.db.execute("SELECT kind FROM capabilities WHERE harness=? AND session=? AND agent=?", (*self.identity, agent))}
        if not capable:
            raise ValueError("Register this member before claiming work")
        member = self.db.execute("SELECT state FROM members WHERE harness=? AND session=? AND agent=?", (*self.identity, agent)).fetchone()
        if member is None or member[0] != "working":
            raise ValueError("Register or resume this member before claiming work")
        rows = self.ready()
        history = self.db.execute("""SELECT w.topic,w.environment FROM work w JOIN work_participants p
            ON p.harness=w.harness AND p.session=w.session AND p.work=w.id
            WHERE w.harness=? AND w.session=? AND p.agent=? AND w.state IN ('active','done')""", (*self.identity, agent)).fetchall()
        topics = {r[0] for r in history if r[0]}
        environments = {r[1] for r in history if r[1]}
        eligible = [row for row in rows if self.eligible(row, agent, capable)]
        if not eligible:
            return None
        frontier = self.db.execute("""SELECT MAX(w.created) FROM work w JOIN work_participants p
            ON p.harness=w.harness AND p.session=w.session AND p.work=w.id
            WHERE w.harness=? AND w.session=? AND p.agent=?""", (*self.identity, agent)).fetchone()[0]
        # Once familiar work overtakes older ready work, drain that finite older
        # cohort before considering newer arrivals. Affinity cannot starve cold work.
        older = [row for row in eligible if frontier is not None and row["created"] < frontier]
        row = min(older or eligible, key=lambda r: (r["topic"] not in topics, r["environment"] not in environments, r["created"], r["id"]))
        self.db.execute("UPDATE work SET state='active',owner=? WHERE harness=? AND session=? AND id=? AND state='ready'", (agent, *self.identity, row["id"]))
        self.db.execute("INSERT OR IGNORE INTO work_participants VALUES (?,?,?,?)", (*self.identity, row["id"], agent))
        self.db.execute("UPDATE members SET state='working' WHERE harness=? AND session=? AND agent=?", (*self.identity, agent))
        result = dict(row)
        result.update(state="active", owner=agent)
        result["dependencies"] = [dict(r) for r in self.db.execute("""SELECT p.id,p.result FROM dependencies d JOIN work p
            ON p.harness=d.harness AND p.session=d.session AND p.id=d.dependency
            WHERE d.harness=? AND d.session=? AND d.work=?""", (*self.identity, row["id"]))]
        return result

    def ready(self) -> list[sqlite3.Row]:
        """Read work whose dependencies are complete for both claims and targeted notices."""
        return self.db.execute("""SELECT w.* FROM work w WHERE w.harness=? AND w.session=? AND w.state='ready'
            AND NOT EXISTS (SELECT 1 FROM dependencies d JOIN work p
                ON p.harness=d.harness AND p.session=d.session AND p.id=d.dependency
                WHERE d.harness=w.harness AND d.session=w.session AND d.work=w.id AND p.state!='done')
            ORDER BY created,id""", self.identity).fetchall()

    def eligible(self, work: sqlite3.Row, agent: str, capabilities: set[str]) -> bool:
        """Apply the same capability and independent-review rules to admission and notification."""
        excluded = self.db.execute("SELECT 1 FROM exclusions WHERE harness=? AND session=? AND work=? AND agent=?",
                                   (*self.identity, work["id"], agent)).fetchone()
        return (work["kind"] in capabilities and not excluded
                and (work["kind"] != "review" or agent not in self.contributors(work["id"])))

    def notifications(self, work_ids: list[str], sender: str) -> list[dict[str, Any]]:
        """Route ready-work notices to a familiar member and an available alternative, without reserving work.

        The caller delivers these compact routes through native messaging/follow-up.
        Recorded work ownership estimates availability; the harness owns actual activity.
        """
        members = self.db.execute("SELECT agent,state FROM members WHERE harness=? AND session=? AND state!='done' AND agent!=?",
                                  (*self.identity, sender)).fetchall()
        candidates = []
        for member in members:
            agent = member["agent"]
            capabilities = {r[0] for r in self.db.execute(
                "SELECT kind FROM capabilities WHERE harness=? AND session=? AND agent=?", (*self.identity, agent))}
            history = self.db.execute("""SELECT w.topic,w.environment FROM work w JOIN work_participants p
                ON p.harness=w.harness AND p.session=w.session AND p.work=w.id
                WHERE w.harness=? AND w.session=? AND p.agent=?""", (*self.identity, agent)).fetchall()
            active = self.db.execute("SELECT COUNT(*) FROM work WHERE harness=? AND session=? AND owner=? AND state='active'",
                                     (*self.identity, agent)).fetchone()[0]
            candidates.append((agent, member["state"], capabilities, {r[0] for r in history if r[0]},
                               {r[1] for r in history if r[1]}, active))
        routed: dict[str, list[str]] = {}
        for work in self.ready():
            if work["id"] not in work_ids:
                continue
            eligible = [member for member in candidates
                        if self.eligible(work, member[0], member[2])
                        and (member[1] != "waiting" or work["creator"] == member[0])]
            if not eligible:
                continue
            ordered = sorted(eligible, key=lambda m: (
                work["creator"] != m[0], m[1] != "waiting", work["topic"] not in m[3],
                work["environment"] not in m[4], m[5], m[0],
            ))
            recipients = [ordered[0]]
            idle = next((member for member in ordered if member[1] == "working" and member[5] == 0), None)
            if idle is not None and idle[0] != recipients[0][0]:
                recipients.append(idle)
            for agent, *_ in recipients:
                routed.setdefault(agent, []).append(work["id"])
        return [{"agent": agent, "ready": work} for agent, work in routed.items()]

    def dependants(self, work: str) -> list[str]:
        """Locate consumers whose readiness may have changed when an input completes."""
        return [r[0] for r in self.db.execute(
            "SELECT work FROM dependencies WHERE harness=? AND session=? AND dependency=?", (*self.identity, work))]

    def contributors(self, work: str) -> set[str]:
        """Exclude Worker and Experimenter participants, including previous owners after handoff."""
        rows = self.db.execute("""WITH RECURSIVE ancestry(id) AS (
            SELECT ? UNION SELECT d.dependency FROM dependencies d JOIN ancestry a ON d.work=a.id
            WHERE d.harness=? AND d.session=?)
            SELECT p.agent FROM work_participants p JOIN ancestry a ON p.work=a.id
            JOIN work w ON w.harness=p.harness AND w.session=p.session AND w.id=p.work
            WHERE p.harness=? AND p.session=? AND w.kind IN ('worker','experimenter')""", (work, *self.identity, *self.identity))
        return {row[0] for row in rows}

    def finish(self, work: str, agent: str, result: Path) -> None:
        """Complete owned work with an original artifact or analysis; success is judged in that artifact."""
        path = result.resolve(strict=True)
        cursor = self.db.execute("""UPDATE work SET state='done',result=?,reason=NULL
            WHERE harness=? AND session=? AND id=? AND state='active' AND owner=?""", (str(path), *self.identity, work, agent))
        if not cursor.rowcount:
            raise ValueError("Only the active owner can finish this work")

    def handoff(self, work: str, agent: str, reason: str, from_agent: str | None = None) -> None:
        """Return work for recovery; a peer names the confirmed stopped owner in a compare-and-set."""
        previous = from_agent or agent
        cursor = self.db.execute("""UPDATE work SET state='ready',owner=NULL,previous_owner=?,reason=?
            WHERE harness=? AND session=? AND id=? AND state='active' AND owner=?""", (previous, reason, *self.identity, work, previous))
        if not cursor.rowcount:
            raise ValueError("The expected active owner no longer owns this work")

    def idle_action(self, agent: str) -> dict[str, str]:
        """Give every continuing member a substantive next action when the queue is empty."""
        task = self.active()
        role = self.db.execute("SELECT role FROM members WHERE harness=? AND session=? AND agent=?",
                               (*self.identity, agent)).fetchone()
        role_name = role[0] if role else "member"
        instruction = (
            f"Continue your {role_name} responsibility from the latest retained configuration and unresolved task questions. "
            "Claim eligible ready work first; if none is ready, choose the next substantive investigation or validation "
            "within the task scope, record the evidence, and submit the next implementation or experiment stage when needed. "
            "An empty queue is not a dependency. Release as waiting only for a concrete unavailable prerequisite, "
            "or done only when this responsibility is actually answered."
        )
        return {"action": "research", "task": task["task"], "instruction": instruction}

    def item(self, work: str) -> dict[str, Any]:
        """Return one changed work item without replaying the task's history."""
        return dict(self.db.execute("SELECT * FROM work WHERE harness=? AND session=? AND id=?", (*self.identity, work)).fetchone())

    def status(self) -> list[dict[str, Any]]:
        """Expose work ownership and results without inferring native member activity."""
        return [dict(row) for row in self.db.execute("SELECT * FROM work WHERE harness=? AND session=? ORDER BY created,id", self.identity)]
