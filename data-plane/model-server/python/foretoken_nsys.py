# SPDX-License-Identifier: Apache-2.0
# SPDX-FileCopyrightText: Copyright contributors to the Foretoken project

"""Inspect GPU activity in an Nsight SQLite export."""

import json
from pathlib import Path
import sqlite3
import sys


def has_gpu_activity(database: Path) -> bool:
    """Return whether the runtime's SQLite export contains GPU kernels."""
    with sqlite3.connect(database.as_uri() + "?mode=ro", uri=True) as connection:
        table = connection.execute(
            "SELECT name FROM sqlite_master WHERE type='table' AND name='CUPTI_ACTIVITY_KIND_KERNEL'"
        ).fetchone()
        return table is not None and connection.execute(
            "SELECT EXISTS(SELECT 1 FROM CUPTI_ACTIVITY_KIND_KERNEL)"
        ).fetchone()[0] == 1


if __name__ == "__main__":
    print(json.dumps(has_gpu_activity(Path(sys.argv[1]))))
