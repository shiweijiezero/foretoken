# SPDX-License-Identifier: Apache-2.0
# SPDX-FileCopyrightText: Copyright contributors to the Foretoken project

"""Validate prior evaluation identity and dispatch an isolated framework process."""

from __future__ import annotations

import json
from pathlib import Path
import sys


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
    runner(
        invocation["arguments"], invocation["service"], invocation["directory"],
        resume=bool(invocation["resume"]),
    )


if __name__ == "__main__":
    main()
