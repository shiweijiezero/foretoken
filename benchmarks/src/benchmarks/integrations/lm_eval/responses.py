# SPDX-License-Identifier: Apache-2.0
# SPDX-FileCopyrightText: Copyright contributors to the Foretoken project

"""Retain individual lm-eval generations and completed token-likelihood scores."""

from __future__ import annotations

import json
import logging
import shutil
import sqlite3
import tempfile
from collections import defaultdict, deque
from pathlib import Path
from typing import Any, NamedTuple, Self

logger = logging.getLogger(__name__)


def restore_progress(source: Path, native: Path) -> None:
    """Stage a closed response archive for the new run, leaving the previous run unchanged."""
    database = source / LmEvalResponses.filename
    if not database.is_file():
        raise ValueError("The previous lm-eval run has no saved evaluation progress")
    shutil.copyfile(database, native.parent / LmEvalResponses.filename)


class ResponseSlot(NamedTuple):
    """Pair a completion callback with its position in the harness's requested samples."""

    request: int
    sample: int


class LmEvalResponses:
    """Own node-local response writes and archive completed work when the run exits."""

    filename = "lm_eval_responses.sqlite"

    def __init__(self, directory: Path) -> None:
        """Open an isolated working database, seeded from this run's restored archive if present."""
        self.archive = directory / self.filename
        # TMPDIR may point at shared storage; response transactions belong on the execution node.
        self.local_directory = Path(tempfile.mkdtemp(prefix="foretoken-lm-eval-", dir="/tmp"))
        self.database = self.local_directory / self.filename
        self.consumed: dict[int, int] = {}
        self.pending: dict[tuple[str, Any], deque[ResponseSlot]] = defaultdict(deque)
        connection = None
        try:
            if self.archive.is_file():
                shutil.copyfile(self.archive, self.database)
            connection = sqlite3.connect(self.database)
            connection.executescript("""
                CREATE TABLE IF NOT EXISTS requests (id INTEGER PRIMARY KEY, arguments TEXT NOT NULL UNIQUE);
                CREATE TABLE IF NOT EXISTS responses (
                    request INTEGER NOT NULL, sample INTEGER NOT NULL, response TEXT NOT NULL,
                    PRIMARY KEY (request, sample)
                );
                CREATE TABLE IF NOT EXISTS likelihoods (
                    request TEXT PRIMARY KEY, loglikelihood REAL NOT NULL, is_greedy INTEGER NOT NULL
                );
            """)
        except BaseException:
            if connection is not None:
                connection.close()
            shutil.rmtree(self.local_directory)
            raise
        self.connection = connection

    def __enter__(self) -> Self:
        return self

    def __exit__(self, exc_type: object, error: BaseException | None, traceback: object) -> None:
        """Close before publishing an archive; retain local progress if publication fails."""
        try:
            self.connection.close()
            # Publish only a complete, closed SQLite file, never a database with active writers.
            with tempfile.NamedTemporaryFile(
                prefix=".lm-eval-responses-", suffix=".sqlite", dir=self.archive.parent, delete=False,
            ) as staged:
                staged_path = Path(staged.name)
            try:
                shutil.copyfile(self.database, staged_path)
                staged_path.replace(self.archive)
            finally:
                staged_path.unlink(missing_ok=True)
            shutil.rmtree(self.local_directory)
        except (OSError, sqlite3.Error) as archive_error:
            message = f"Could not finalize lm-eval progress; local database path: {self.database}"
            logger.exception(message)
            if error is None:
                raise
            error.add_note(f"{message}: {archive_error}")

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

    def get_likelihood(self, request: str) -> tuple[float, bool] | None:
        """Read a completed token-window score by its explicit request and scoring settings."""
        row = self.connection.execute(
            "SELECT loglikelihood, is_greedy FROM likelihoods WHERE request = ?", (request,),
        ).fetchone()
        return None if row is None else (row[0], bool(row[1]))

    def add_partial(self, attr: str, req: Any, res: Any) -> None:
        """Commit native callbacks to independent likelihood scores or ordered generation slots.

        Likelihood calls carry a normalized scoring key. Generations retain their sample
        slot through retries; identical sampled answers remain separate completions.
        """
        with self.connection:
            if attr == "loglikelihood":
                self.connection.execute("INSERT INTO likelihoods VALUES (?, ?, ?)", (req, *res))
            else:
                slot = req if isinstance(req, ResponseSlot) else self.claim(req)
                self.connection.execute("INSERT INTO responses VALUES (?, ?, ?)", (*slot, res))
