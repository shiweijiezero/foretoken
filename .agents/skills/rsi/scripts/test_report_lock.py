# SPDX-License-Identifier: Apache-2.0
# SPDX-FileCopyrightText: Copyright contributors to the Foretoken project

"""Check the task-local report lock wrapper and child exit propagation."""

from __future__ import annotations

import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

SCRIPT = Path(__file__).with_name("report_lock.py")


class ReportLockTest(unittest.TestCase):
    """Protect the externally visible lock contract without testing an editor."""

    def test_child_runs_under_report_lock(self) -> None:
        """The wrapped updater sees the report path and leaves a lock file behind."""
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            report = root / "report.md"
            marker = root / "marker"
            result = subprocess.run([
                sys.executable, str(SCRIPT), "--report", str(report), "--", sys.executable, "-c",
                f"from pathlib import Path; Path({str(marker)!r}).write_text('ok', encoding='utf-8')",
            ], check=False)
            self.assertEqual(result.returncode, 0)
            self.assertEqual(marker.read_text(encoding="utf-8"), "ok")
            self.assertTrue((root / ".foretoken-report.lock").is_file())

    def test_child_failure_is_returned(self) -> None:
        """A failed updater is visible to the owner instead of being swallowed."""
        with tempfile.TemporaryDirectory() as directory:
            result = subprocess.run([
                sys.executable, str(SCRIPT), "--report", str(Path(directory) / "report.md"), "--",
                sys.executable, "-c", "raise SystemExit(7)",
            ], check=False)
            self.assertEqual(result.returncode, 7)


if __name__ == "__main__":
    unittest.main()
