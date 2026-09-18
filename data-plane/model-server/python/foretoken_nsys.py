# SPDX-License-Identifier: Apache-2.0
# SPDX-FileCopyrightText: Copyright contributors to the Foretoken project

"""读取 Nsight SQLite 导出的 GPU 活动，并同步发布所需的原生文件。"""

import json
import os
from pathlib import Path
import sqlite3
import sys


def inspect_report(report: Path) -> bool:
    """为 runtime 发布阶段检查已导出的数据库，返回是否记录到 GPU kernel。"""
    database = report.with_suffix(".sqlite")
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
