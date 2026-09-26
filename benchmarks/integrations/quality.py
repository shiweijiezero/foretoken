# SPDX-License-Identifier: Apache-2.0
# SPDX-FileCopyrightText: Copyright contributors to the Foretoken project

"""Restore evaluation progress and dispatch an isolated framework process."""

from __future__ import annotations

from contextlib import closing
import json
import os
from pathlib import Path
import shutil
import sqlite3
import sys
from typing import Any

from benchmarks.config.evaluation import native_arguments, validate_model_transport
from benchmarks.integrations.lm_eval_responses import LmEvalResponses


def _run_evalscope(
    arguments: list[str], service: dict[str, Any], directory: str, *, resume: bool = False
) -> None:
    """Use EvalScope's native configuration and runner for the selected HTTP service."""
    from evalscope.config import parse_task_config
    from evalscope.run import run_task

    args = native_arguments(
        "evalscope",
        [
            *arguments,
            "--model",
            service["model"],
            "--api-url",
            service["api_root"],
            "--eval-type",
            "openai_api",
            "--work-dir",
            directory,
            "--no-timestamp",
        ],
    )
    config = parse_task_config(args)
    if resume:
        if config.use_cache is not None:
            raise ValueError("Use --resume or native --use-cache, not both")
        config.use_cache = directory
    validate_model_transport(config.model_args)
    config.model = service["model"]
    config.api_url = service["api_root"]
    config.eval_type = "openai_api"
    config.work_dir = directory
    config.no_timestamp = True
    # The evaluation child owns its environment; credentials do not enter native reports.
    os.environ["EVALSCOPE_API_KEY"] = service["api_key"]
    config.api_key = None
    headers = dict(config.model_args.get("default_headers") or {})
    headers.update(service["headers"])
    if headers:
        config.model_args["default_headers"] = headers
    reports = run_task(config)
    # A resumed run writes into its cache. Export only this invocation's returned reports.
    if Path(config.work_dir).resolve() != Path(directory).resolve():
        for task, report in reports.items():
            if not report:
                continue
            destination = Path(directory) / "reports" / report.model_name
            destination.mkdir(parents=True, exist_ok=True)
            (destination / f"{task}.json").write_text(
                json.dumps(report.to_dict(), ensure_ascii=False, indent=2),
                encoding="utf-8",
            )
            subsets = {
                subset.name
                for metric in report.metrics
                for category in metric.categories
                for subset in category.subsets
            }
            for kind in ("predictions", "reviews"):
                for subset in subsets:
                    relative = Path(kind) / report.model_name / f"{task}_{subset}.jsonl"
                    source = Path(config.work_dir) / relative
                    if source.exists():
                        target = Path(directory) / relative
                        target.parent.mkdir(parents=True, exist_ok=True)
                        shutil.copy2(source, target)


def _restore_evaluation_progress(evaluator: str, model: str, source: str, native: Path) -> None:
    """Restore framework-specific progress without modifying the previous evaluation."""
    previous_directory = Path(source).expanduser().resolve()
    previous = json.loads((previous_directory / "config.json").read_text(encoding="utf-8"))
    if previous.get("mode") != "evaluation" or previous.get("evaluator") != evaluator:
        raise ValueError("--resume requires a quality evaluation using the same evaluator")
    if previous.get("model") != model:
        raise ValueError("--resume requires the same served model as the previous evaluation")
    cache = previous_directory / "native"
    if evaluator == "lm-eval":
        database = previous_directory / LmEvalResponses.filename
        if not database.is_file():
            raise ValueError("The previous lm-eval run has no saved evaluation progress")
        with closing(sqlite3.connect(database.as_uri() + "?mode=ro", uri=True)) as src:
            with closing(sqlite3.connect(native.parent / LmEvalResponses.filename)) as dst:
                src.backup(dst)
    else:
        if not (cache / "configs" / "task_config.yaml").is_file():
            raise ValueError("The previous EvalScope run has no resumable task configuration")
        for name in ("configs", "predictions", "reviews"):
            if (cache / name).is_dir():
                shutil.copytree(cache / name, native / name)


def main() -> None:
    """Receive an invocation through stdin and run only the selected framework."""
    invocation = json.load(sys.stdin)
    if invocation["resume"]:
        _restore_evaluation_progress(
            invocation["evaluator"], invocation["service"]["model"],
            invocation["resume"], Path(invocation["directory"]),
        )
    if invocation["evaluator"] == "lm-eval":
        from benchmarks.integrations.lm_eval import run_lm_eval

        runner = run_lm_eval
    else:
        runner = _run_evalscope
    runner(
        invocation["arguments"], invocation["service"], invocation["directory"],
        resume=bool(invocation["resume"]),
    )


if __name__ == "__main__":
    main()
