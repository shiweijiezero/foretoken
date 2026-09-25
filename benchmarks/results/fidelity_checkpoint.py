# SPDX-License-Identifier: Apache-2.0
# SPDX-FileCopyrightText: Copyright contributors to the Foretoken project

"""Retain complete comparison windows independently of result publication."""

from __future__ import annotations

import json
import sqlite3
from contextlib import closing
from pathlib import Path
from typing import Any

import numpy as np


class FidelityCheckpoint:
    """Own atomic window checkpoints in one result directory; resume from a read-only snapshot."""

    def __init__(self, directory: Path, resume: str, settings: dict[str, Any]) -> None:
        self.connection = sqlite3.connect(directory / "comparison.sqlite")
        try:
            if resume:
                source = Path(resume).expanduser().resolve() / "comparison.sqlite"
                if not source.is_file():
                    raise ValueError("--resume requires a result directory containing comparison.sqlite")
                with closing(sqlite3.connect(source.as_uri() + "?mode=ro", uri=True)) as previous:
                    previous.backup(self.connection)
                if self.get("settings") != settings:
                    raise ValueError("Resume requires the same reference, candidates, and scoring options")
            else:
                self.connection.executescript("""
                    CREATE TABLE metadata (name TEXT PRIMARY KEY, value TEXT NOT NULL);
                    CREATE TABLE reference_windows (window INTEGER PRIMARY KEY, probabilities BLOB NOT NULL);
                    CREATE TABLE candidate_windows (
                        candidate INTEGER NOT NULL, window INTEGER NOT NULL, scores TEXT NOT NULL,
                        PRIMARY KEY (candidate, window)
                    );
                """)
                self.put("settings", settings)
        except BaseException:
            self.connection.close()
            raise

    def __enter__(self) -> FidelityCheckpoint:
        return self

    def __exit__(self, *args: object) -> None:
        self.connection.close()

    def get(self, name: str) -> Any:
        """Read saved sampling or model metadata, returning None before that stage has started."""
        row = self.connection.execute("SELECT value FROM metadata WHERE name = ?", (name,)).fetchone()
        return None if row is None else json.loads(row[0])

    def put(self, name: str, value: Any) -> None:
        """Commit stage metadata before any windows can refer to it."""
        with self.connection:
            self.connection.execute(
                "INSERT OR REPLACE INTO metadata VALUES (?, ?)", (name, json.dumps(value))
            )

    def reference_done(self) -> set[int]:
        """Return reference windows whose complete probability matrices are committed."""
        return {row[0] for row in self.connection.execute("SELECT window FROM reference_windows")}

    def save_reference(self, window: int, values: np.ndarray) -> None:
        """Commit one full window as little-endian float64 probabilities, never a partial request batch."""
        with self.connection:
            self.connection.execute(
                "INSERT INTO reference_windows VALUES (?, ?)",
                (window, values.astype("<f8", copy=False).tobytes()),
            )

    def reference(self, window: int, shape: tuple[int, int]) -> np.ndarray:
        """Load the committed reference window with the shape recorded by its scoring protocol."""
        row = self.connection.execute(
            "SELECT probabilities FROM reference_windows WHERE window = ?", (window,)
        ).fetchone()
        if row is None:
            raise ValueError(f"Reference window {window} is not complete")
        return np.frombuffer(row[0], dtype="<f8").reshape(shape)

    def candidate_windows(self, candidate: int) -> dict[int, list[dict[str, Any]]]:
        """Read complete candidate windows for skipping work and rebuilding scalar reports."""
        return {
            window: json.loads(scores)
            for window, scores in self.connection.execute(
                "SELECT window, scores FROM candidate_windows WHERE candidate = ? ORDER BY window", (candidate,)
            )
        }

    def save_candidate(self, candidate: int, window: int, rows: list[dict[str, Any]]) -> None:
        """Commit all scored positions of a candidate window in a single transaction."""
        with self.connection:
            self.connection.execute(
                "INSERT INTO candidate_windows VALUES (?, ?, ?)",
                (candidate, window, json.dumps(rows)),
            )
