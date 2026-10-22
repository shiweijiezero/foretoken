# SPDX-License-Identifier: Apache-2.0
# SPDX-FileCopyrightText: Copyright contributors to the Foretoken project

"""Commit run facts and completed requests independently of their exported files."""

from __future__ import annotations

import json
import os
import sqlite3
from collections.abc import Iterable, Mapping
from contextlib import closing
from pathlib import Path
from tempfile import TemporaryDirectory
from threading import Lock
from typing import Any

from benchmarks.results.recording.files import write_json

DATABASE_FILENAME = "records.sqlite3"
_SCHEMA_VERSION = 1
_DOCUMENT_FILES = (
    "config.json", "context.json", "metrics.json", "publication.json", "task.json",
    "wandb.json", "warmup_metrics.json", "sweep_points.json", "sweep_summary.json", "slo_results.json",
)
_REQUEST_CHANNELS = ("raw_output", "warmup_raw_output", "raw_results", "evaluation_progress")
_SCHEMA = """
CREATE TABLE documents (
    name TEXT PRIMARY KEY,
    payload TEXT NOT NULL
);
CREATE TABLE requests (
    id INTEGER PRIMARY KEY,
    channel TEXT NOT NULL,
    payload TEXT NOT NULL
);
CREATE INDEX requests_by_channel ON requests(channel, id);
PRAGMA user_version = 1;
"""


class RunRecords:
    """Own one run's SQLite connection while its caller holds the publication lock.

    Metadata checkpoints commit together. Request inserts commit before their
    JSONL export. A connection lock serializes native evaluator progress with
    the main recording thread. Rollback journaling avoids WAL shared memory
    for result directories that are resumed from another host.
    """

    def __init__(self, directory: Path) -> None:
        """Open the caller's result store, adopting earlier JSON records when necessary."""
        self.directory = directory
        self.path = directory / DATABASE_FILENAME
        self._lock = Lock()
        if not self.path.exists():
            self._initialize()
        self.connection = sqlite3.connect(self.path, check_same_thread=False)
        try:
            _check_version(self.connection, self.path)
        except BaseException:
            self.connection.close()
            raise

    def save(self, documents: Mapping[str, Any]) -> None:
        """Commit related run facts in one transaction before publishing their exports."""
        encoded = [(name, _encode(value)) for name, value in documents.items()]
        with self._lock, self.connection:
            self.connection.executemany(
                "INSERT INTO documents(name, payload) VALUES (?, ?) "
                "ON CONFLICT(name) DO UPDATE SET payload = excluded.payload",
                encoded,
            )

    def publish(self, documents: Mapping[str, Any]) -> dict[str, Path]:
        """Commit a checkpoint before materializing its user-readable JSON exports."""
        self.save(documents)
        return {
            name: write_json(str(self.directory), name, value)
            for name, value in documents.items()
        }

    def read(self, names: Iterable[str]) -> dict[str, Any]:
        """Read a consistent set of retained facts for recording or publication."""
        with self._lock:
            return _read_documents(self.connection, names)

    def export(self) -> None:
        """Recreate metadata and live journals after recording stops, without changing facts."""
        for name, value in self.read(_DOCUMENT_FILES).items():
            write_json(str(self.directory), name, value)
        with self._lock:
            # Export filenames come from the recorder's schema, not from stored path strings.
            channels = [row[0] for row in self.connection.execute("SELECT DISTINCT channel FROM requests")
                        if row[0] in _REQUEST_CHANNELS]
            for channel in channels:
                path = self.directory / f"{channel}.jsonl"
                temporary = path.with_name(f".{path.name}.tmp")
                with temporary.open("w", encoding="utf-8") as target:
                    for (payload,) in self.connection.execute(
                        "SELECT payload FROM requests WHERE channel = ? ORDER BY id", (channel,),
                    ):
                        target.write(payload + "\n")
                temporary.replace(path)

    def start_requests(self, channel: str) -> None:
        """Start the current measurement channel when its recorder opens a new journal."""
        with self._lock, self.connection:
            self.connection.execute("DELETE FROM requests WHERE channel = ?", (channel,))

    def append_request(self, channel: str, record: dict[str, Any]) -> str:
        """Retain one completed request and return its JSON for the live journal export."""
        encoded = _encode(record)
        with self._lock, self.connection:
            self.connection.execute(
                "INSERT INTO requests(channel, payload) VALUES (?, ?)", (channel, encoded),
            )
        return encoded

    def requests(self, channel: str) -> list[dict[str, Any]]:
        """Read one measurement channel in completion order without depending on exports."""
        with self._lock:
            return _read_requests(self.connection, channel)

    def close(self) -> None:
        """Release the database before the owning run releases its publication lock."""
        with self._lock:
            self.connection.close()

    def _initialize(self) -> None:
        """Adopt existing JSON results once and publish only a complete database."""
        documents = {
            name: json.loads(path.read_text(encoding="utf-8"))
            for name in _DOCUMENT_FILES
            if (path := self.directory / name).is_file()
        }
        # Construct privately: failed adoption leaves the original exports usable.
        with TemporaryDirectory(prefix=".records-", dir=self.directory) as temporary:
            staged = Path(temporary) / "pending.sqlite3"
            with closing(sqlite3.connect(staged)) as connection:
                connection.executescript(_SCHEMA)
                with connection:
                    connection.executemany(
                        "INSERT INTO documents(name, payload) VALUES (?, ?)",
                        ((name, _encode(value)) for name, value in documents.items()),
                    )
                    for channel in _REQUEST_CHANNELS:
                        connection.executemany(
                            "INSERT INTO requests(channel, payload) VALUES (?, ?)",
                            ((channel, _encode(row)) for row in _legacy_requests(self.directory, channel)),
                        )
            os.replace(staged, self.path)


def read_records(directory: Path, names: Iterable[str]) -> dict[str, Any]:
    """Read canonical facts or earlier JSON results, letting SQLite recover an interrupted commit.

    ``mode=rw`` opens only an existing database and permits hot-journal rollback;
    SQLite can still read a clean database when its file is write-protected.
    """
    path = directory / DATABASE_FILENAME
    if path.is_file():
        with closing(sqlite3.connect(path.resolve().as_uri() + "?mode=rw", uri=True)) as connection:
            _check_version(connection, path)
            return _read_documents(connection, names)
    return {
        name: json.loads(path.read_text(encoding="utf-8"))
        for name in names
        if (path := directory / name).is_file()
    }


def read_record(directory: Path, name: str) -> Any:
    """Read a required run document, reporting a missing fact rather than an empty record."""
    documents = read_records(directory, (name,))
    if name not in documents:
        raise FileNotFoundError(f"Result record {name} is missing in {directory}")
    return documents[name]


def write_records(directory: Path, documents: Mapping[str, Any]) -> dict[str, Path]:
    """Commit and export saved-run metadata while the caller owns its publication lock."""
    with closing(RunRecords(directory)) as records:
        return records.publish(documents)


def read_recorded_requests(directory: Path, channel: str) -> list[dict[str, Any]]:
    """Read measured completions from SQLite or an earlier published JSON/JSONL result."""
    path = directory / DATABASE_FILENAME
    if path.is_file():
        with closing(sqlite3.connect(path.resolve().as_uri() + "?mode=rw", uri=True)) as connection:
            _check_version(connection, path)
            return _read_requests(connection, channel)
    return list(_legacy_requests(directory, channel))


def _read_documents(connection: sqlite3.Connection, names: Iterable[str]) -> dict[str, Any]:
    names = tuple(names)
    if not names:
        return {}
    placeholders = ",".join("?" for _ in names)
    return {
        name: json.loads(payload)
        for name, payload in connection.execute(
            f"SELECT name, payload FROM documents WHERE name IN ({placeholders})", names,
        )
    }


def _read_requests(connection: sqlite3.Connection, channel: str) -> list[dict[str, Any]]:
    return [
        json.loads(payload)
        for (payload,) in connection.execute(
            "SELECT payload FROM requests WHERE channel = ? ORDER BY id", (channel,),
        )
    ]


def _legacy_requests(directory: Path, channel: str) -> Iterable[dict[str, Any]]:
    """Load the existing result format only when no authoritative database is present."""
    published = directory / f"{channel}.json"
    if published.is_file():
        yield from json.loads(published.read_text(encoding="utf-8"))
        return
    journal = directory / f"{channel}.jsonl"
    if journal.is_file():
        with journal.open(encoding="utf-8") as source:
            for line in source:
                if line.strip():
                    yield json.loads(line)


def _check_version(connection: sqlite3.Connection, path: Path) -> None:
    version = connection.execute("PRAGMA user_version").fetchone()[0]
    if version != _SCHEMA_VERSION:
        raise ValueError(f"Unsupported result database version {version} in {path}")


def _encode(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, separators=(",", ":"))
