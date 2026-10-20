# SPDX-License-Identifier: Apache-2.0
# SPDX-FileCopyrightText: Copyright contributors to the Foretoken project

"""Dispatch isolated evaluators and share completion snapshots and credential protection."""

from __future__ import annotations

import json
import sys
from pathlib import Path
from typing import Any

PROGRESS_FILENAME = "evaluation_progress.json"


class EvaluationProgress:
    """Publish measured counters from the isolated evaluator for its parent process."""

    def __init__(self, directory: str) -> None:
        self.path = Path(directory) / PROGRESS_FILENAME
        self.counts: dict[str, int] = {}

    def advance(self, metric: str, count: int) -> None:
        """Count completed or reused work at the native response boundary."""
        self.counts[metric] = self.counts.get(metric, 0) + count
        self.flush()

    def flush(self) -> None:
        """Atomically expose the latest counters, including work completed before exit."""
        write_progress(self.path.parent, self.counts)


def main() -> None:
    """Receive an invocation through stdin and run only the selected framework."""
    invocation = json.load(sys.stdin)
    if invocation["resume"]:
        _restore_evaluation_progress(
            invocation["evaluator"], invocation["service"]["model"],
            invocation["resume"], Path(invocation["directory"]),
        )
    if invocation["evaluator"] == "lm-eval":
        from benchmarks.integrations.lm_eval.runner import run_lm_eval

        runner = run_lm_eval
    else:
        from benchmarks.integrations.evalscope.evaluation import run_evalscope

        runner = run_evalscope
    progress = EvaluationProgress(invocation["directory"])
    try:
        runner(
            invocation["arguments"], invocation["service"], invocation["directory"],
            resume=bool(invocation["resume"]),
            **({"progress": progress} if invocation["evaluator"] == "lm-eval" else {}),
        )
    finally:
        if invocation["evaluator"] == "lm-eval":
            progress.flush()


def read_progress(directory: Path) -> dict[str, int] | None:
    """Read published completion counters for the parent poller without loading an evaluator."""
    path = directory / PROGRESS_FILENAME
    if not path.is_file():
        return None
    return json.loads(path.read_text(encoding="utf-8"))


def write_progress(directory: Path, snapshot: dict) -> None:
    """Replace the child snapshot atomically so the parent never reads a partial record."""
    path = directory / PROGRESS_FILENAME
    temporary = path.with_suffix(".tmp")
    temporary.write_text(json.dumps(snapshot), encoding="utf-8")
    temporary.replace(path)


def validate_model_transport(arguments: dict[str, Any]) -> None:
    """Keep service credentials out of native model options before either framework records them."""
    for field in ("api_key", "auth_token"):
        if arguments.get(field):
            raise ValueError(
                f"Model argument {field} is reserved; supply authentication using `--api-key`"
            )
    for field in ("header", "default_headers"):
        headers = arguments.get(field) or {}
        if any(name.lower() in ("authorization", "proxy-authorization") for name in headers):
            raise ValueError(
                "Authorization headers are reserved; supply authentication using `--api-key`"
            )


def _restore_evaluation_progress(evaluator: str, model: str, source: str, native: Path) -> None:
    """Validate the previous evaluation before delegating its framework-specific restoration."""
    previous_directory = Path(source).expanduser().resolve()
    previous = json.loads((previous_directory / "config.json").read_text(encoding="utf-8"))
    if previous.get("mode") != "evaluation" or previous.get("evaluator") != evaluator:
        raise ValueError("--resume requires a quality evaluation using the same evaluator")
    if previous.get("model") != model:
        raise ValueError("--resume requires the same served model as the previous evaluation")
    if evaluator == "lm-eval":
        from benchmarks.integrations.lm_eval.responses import restore_progress
    else:
        from benchmarks.integrations.evalscope.evaluation import restore_progress

    restore_progress(previous_directory, native)


if __name__ == "__main__":
    main()
