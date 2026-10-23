# SPDX-License-Identifier: Apache-2.0
# SPDX-FileCopyrightText: Copyright contributors to the Foretoken project

"""Serialize result recording, publication, and figure exports on their owned directories."""

from __future__ import annotations

from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path
from typing import Literal

import portalocker


@contextmanager
def publication_lock(directory: Path, *, scope: Literal["publish", "plots"] = "publish") -> Iterator[None]:
    """Serialize publication or figure exports; distinct scopes may safely share a directory."""
    path = directory / f".foretoken-{scope}.lock"
    try:
        with path.open("a+b") as lock:
            portalocker.lock(lock, portalocker.LOCK_EX)
            try:
                yield
            finally:
                portalocker.unlock(lock)
    except portalocker.exceptions.LockException as error:
        raise OSError(f"Result directory lock failed at {path}: {error}") from error
