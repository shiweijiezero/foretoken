# SPDX-License-Identifier: Apache-2.0
# SPDX-FileCopyrightText: Copyright contributors to the Foretoken project

"""Reserve result directories and atomically materialize their measurement artifacts."""

from __future__ import annotations

import csv
import json
import os
from datetime import datetime
from pathlib import Path
from tempfile import mkdtemp
from typing import Any

from benchmarks.results.types import ResultConfiguration


def result_directory_path(
    benchmark: ResultConfiguration,
    output_dir: str | None = None,
    directory_prefix: str = "",
) -> str:
    """Return an explicit child path, or reserve a unique local directory under ``--output-dir``."""
    if output_dir is not None:
        return output_dir
    timestamp = datetime.now().astimezone().strftime("%Y%m%d_%H%M%S")
    if benchmark.outputs.saves_local:
        os.makedirs(benchmark.outputs.output_dir, exist_ok=True)
        return mkdtemp(
            prefix=f"{directory_prefix}{timestamp}-",
            dir=benchmark.outputs.output_dir,
        )
    return os.path.join(
        benchmark.outputs.output_dir,
        f"{directory_prefix}{timestamp}",
    )


def write_json(directory: str, filename: str, data: Any, *, compact: bool = False) -> Path:
    """Atomically replace a JSON artifact so interrupted writes leave the previous record readable."""
    path = Path(directory) / filename
    temporary = path.with_name(f".{path.name}.tmp")
    with temporary.open("w", encoding="utf-8") as file:
        if compact:
            json.dump(data, file, ensure_ascii=False, separators=(",", ":"))
        else:
            json.dump(data, file, indent=4, ensure_ascii=False)
    temporary.replace(path)
    return path


def write_gpu_allocation_csv(directory: str, allocation: dict[str, Any]) -> Path:
    """Write every GPU snapshot and failed-read boundary for local inspection."""
    resources = sorted({
        resource
        for sample in allocation["samples"]
        for resource in sample["gpu_counts"]
    })
    rows = [
        {
            "elapsed_time_s": sample["elapsed_time_s"],
            "event": "sample",
            **{resource: sample["gpu_counts"].get(resource) for resource in resources},
        }
        for sample in allocation["samples"]
    ]
    rows.extend(
        {
            "elapsed_time_s": elapsed,
            "event": "failed_read",
            **dict.fromkeys(resources),
        }
        for elapsed in allocation["failed_read_elapsed_s"]
    )
    rows.sort(key=lambda row: (float(row["elapsed_time_s"]), row["event"]))
    path = Path(directory) / "gpu_allocation.csv"
    with path.open("w", encoding="utf-8", newline="") as file:
        writer = csv.DictWriter(file, fieldnames=("elapsed_time_s", "event", *resources))
        writer.writeheader()
        writer.writerows(rows)
    return path
