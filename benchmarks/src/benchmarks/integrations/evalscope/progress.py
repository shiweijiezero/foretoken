# SPDX-License-Identifier: Apache-2.0
# SPDX-FileCopyrightText: Copyright contributors to the Foretoken project

"""Read EvalScope prediction and review progress from complete native cache records."""

from __future__ import annotations

import json
import os
from pathlib import Path

from benchmarks.integrations.quality import PROGRESS_FILENAME


class EvalScopeProgress:
    """Retain cache offsets and sample IDs across snapshots without loading EvalScope."""

    def __init__(self) -> None:
        self.files: dict[Path, tuple[tuple[int, int], int, set[int]]] = {}

    def read(self, native: Path) -> dict[str, int] | None:
        """Read unique saved sample counts for the parent poller, including transactional review reruns."""
        path = native / PROGRESS_FILENAME
        if not path.is_file():
            return None
        snapshot = json.loads(path.read_text(encoding="utf-8"))
        directory = Path(snapshot["cache_directory"])
        model = snapshot["model"]
        cache_files = snapshot["cache_files"]
        counts = {}
        for kind, metric in (
            ("predictions", "Evaluation/Predictions completed"),
            ("reviews", "Evaluation/Reviews completed"),
        ):
            total = 0
            cache = directory / kind / model
            selected = {cache / filename for filename in cache_files}
            paths = {path for path in selected if path.is_file()}
            # A review rerun replaces its previous scores only after aggregation succeeds.
            reruns = {Path(str(path).split(".rerun-", 1)[0]): path
                      for path in cache.glob("*.jsonl.rerun-*")
                      if Path(str(path).split(".rerun-", 1)[0]) in selected}
            for path in sorted(set(paths) | set(reruns)):
                source = reruns.get(path, path)
                try:
                    with source.open("rb") as stream:
                        stat = os.fstat(stream.fileno())
                        identity = (stat.st_dev, stat.st_ino)
                        previous, offset, samples = self.files.get(source, (identity, 0, set()))
                        # Native review commits atomically replace the file at the same path.
                        if previous != identity:
                            offset, samples = 0, set()
                        stream.seek(offset)
                        while line := stream.readline():
                            if not line.endswith(b"\n"):
                                break
                            samples.add(json.loads(line)["index"])
                            offset = stream.tell()
                except FileNotFoundError:
                    # Native aggregation can rename a review rerun during this snapshot.
                    return None
                self.files[source] = identity, offset, samples
                total += len(samples)
            counts[metric] = total
        return counts
