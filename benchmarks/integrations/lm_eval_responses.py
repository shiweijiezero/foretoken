# SPDX-License-Identifier: Apache-2.0
# SPDX-FileCopyrightText: Copyright contributors to the Foretoken project

"""Retain individual lm-eval generations without collapsing repeated sampling requests."""

from __future__ import annotations

from collections import defaultdict, deque
import json
import sqlite3
from pathlib import Path
from typing import Any, NamedTuple


class ResponseSlot(NamedTuple):
    """Pair a completion callback with its position in the harness's requested samples."""

    request: int
    sample: int


class LmEvalResponses:
    """Own a run's completed responses and consume each saved generation once per invocation."""

    filename = "lm_eval_responses.sqlite"

    def __init__(self, directory: Path) -> None:
        self.connection = sqlite3.connect(directory / self.filename)
        self.consumed: dict[int, int] = {}
        self.pending: dict[tuple[str, Any], deque[ResponseSlot]] = defaultdict(deque)
        try:
            self.connection.executescript("""
                CREATE TABLE IF NOT EXISTS requests (id INTEGER PRIMARY KEY, arguments TEXT NOT NULL UNIQUE);
                CREATE TABLE IF NOT EXISTS responses (
                    request INTEGER NOT NULL, sample INTEGER NOT NULL, response TEXT NOT NULL,
                    PRIMARY KEY (request, sample)
                );
            """)
        except BaseException:
            self.connection.close()
            raise

    def __enter__(self) -> LmEvalResponses:
        return self

    def __exit__(self, *args: object) -> None:
        self.connection.close()

    @staticmethod
    def _key(arguments: Any) -> str:
        return json.dumps(arguments, sort_keys=True, ensure_ascii=False)

    @classmethod
    def _dispatch_key(cls, arguments: Any) -> tuple[str, Any]:
        """Match the native batcher's parameter equivalence, including integer/float equality."""
        from lm_eval.models.utils import Collator

        groups = Collator.group([arguments], lambda request: request[1])
        return cls._key(arguments[0]), next(iter(groups))

    def reserve(self, arguments: Any) -> ResponseSlot:
        """Allocate each harness-expanded sample a position and queue any unfinished work."""
        key = self._key(arguments)
        with self.connection:
            self.connection.execute("INSERT OR IGNORE INTO requests (arguments) VALUES (?)", (key,))
            request = self.connection.execute("SELECT id FROM requests WHERE arguments = ?", (key,)).fetchone()[0]
        sample = self.consumed.get(request, 0)
        self.consumed[request] = sample + 1
        slot = ResponseSlot(request, sample)
        if self.get(slot) is None:
            self.pending[self._dispatch_key(arguments)].append(slot)
        return slot

    def claim(self, arguments: Any) -> ResponseSlot:
        """Bind an upstream call to the next missing sample before concurrent execution or retries."""
        return self.pending[self._dispatch_key(arguments)].popleft()

    def get(self, slot: ResponseSlot) -> str | None:
        """Return a completed generation, with None denoting work still needed and empty text preserved."""
        row = self.connection.execute(
            "SELECT response FROM responses WHERE request = ? AND sample = ?", slot,
        ).fetchone()
        return None if row is None else row[0]

    def add_partial(self, attr: str, req: Any, res: str) -> None:
        """Commit one actual completion through lm-eval's callback, retaining identical answers.

        Concurrent calls carry their bound slot through upstream retries. Serial callbacks
        claim in execution order; neither path assigns sample order by response latency.
        """
        slot = req if isinstance(req, ResponseSlot) else self.claim(req)
        with self.connection:
            self.connection.execute("INSERT INTO responses VALUES (?, ?, ?)", (*slot, res))
