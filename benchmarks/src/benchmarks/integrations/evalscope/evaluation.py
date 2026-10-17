# SPDX-License-Identifier: Apache-2.0
# SPDX-FileCopyrightText: Copyright contributors to the Foretoken project

"""Run native EvalScope quality tasks and restore their prediction and review caches."""

from __future__ import annotations

import json
import os
import shutil
from pathlib import Path
from typing import Any

from benchmarks.config.evaluation import native_arguments, validate_model_transport
from benchmarks.integrations.quality import write_progress


class EvalScopeProgress:
    """Read only complete native cache records; prediction and review counts stay separate."""

    def __init__(self) -> None:
        self.files: dict[Path, tuple[tuple[int, int], int, set[int]]] = {}

    def read(self, directory: Path, model: str, cache_files: list[str]) -> dict[str, int] | None:
        """Incrementally count unique saved sample IDs, including transactional review reruns."""
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


def restore_progress(source: Path, native: Path) -> None:
    """Copy an earlier run's native task state into a new evaluation, leaving old reports behind."""
    cache = source / "native"
    if not (cache / "configs" / "task_config.yaml").is_file():
        raise ValueError("The previous EvalScope run has no resumable task configuration")
    for name in ("configs", "predictions", "reviews"):
        if (cache / name).is_dir():
            shutil.copytree(cache / name, native / name)


def run_evalscope(
    arguments: list[str], service: dict[str, Any], directory: str, *, resume: bool = False
) -> None:
    """Use EvalScope's native configuration and runner for the selected HTTP service."""
    from evalscope.api.registry import get_benchmark
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
    # Native --use-cache redirects all output to the selected cache directory.
    write_progress(Path(directory), {
        "cache_directory": str(Path(config.use_cache or directory).expanduser().resolve()),
        "model": config.model_id,
        "cache_files": [
            f"{task}_{subset}.jsonl"
            for task in config.datasets
            for subset in get_benchmark(task, config).benchmark_meta.subset_list
        ],
    })
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
