# SPDX-License-Identifier: Apache-2.0
# SPDX-FileCopyrightText: Copyright contributors to the Foretoken project

"""Validate a stopped Nsight capture using the native exporter and publish GPU activity."""

import json
import os
from pathlib import Path
import sqlite3
import subprocess
import sys


def inspect_report(report: Path) -> bool:
    """Export one native report for runtime sealing; retain the SQLite file for later analysis."""
    database = report.with_suffix(".sqlite")
    subprocess.run(
        ["nsys", "export", "--type=sqlite", f"--output={database}", str(report)],
        check=True,
        stdout=sys.stderr,
    )
    with sqlite3.connect(database.as_uri() + "?mode=ro", uri=True) as connection:
        table = connection.execute(
            "SELECT name FROM sqlite_master WHERE type='table' AND name='CUPTI_ACTIVITY_KIND_KERNEL'"
        ).fetchone()
        active = table is not None and connection.execute(
            "SELECT EXISTS(SELECT 1 FROM CUPTI_ACTIVITY_KIND_KERNEL)"
        ).fetchone()[0] == 1
    for path in (report, database):
        with path.open("rb") as stream:
            os.fsync(stream.fileno())
    return active


if __name__ == "__main__":
    print(json.dumps(inspect_report(Path(sys.argv[1]))))
