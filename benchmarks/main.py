# SPDX-License-Identifier: Apache-2.0
# SPDX-FileCopyrightText: Copyright contributors to the Foretoken project

"""Foretoken benchmark CLI entry point."""

from __future__ import annotations

import asyncio
import logging
import re
import secrets
from collections.abc import Sequence
from datetime import datetime, timezone
from pathlib import Path

from benchmarks.arguments import CleanupCommand, RunCommand, parse_arguments
from benchmarks.kubernetes import cleanup_managed, run_managed
from benchmarks.runner.select_runner import select_runner
from benchmarks.storage.result_writer import validate_run_id, write_json_atomic

logger = logging.getLogger(__name__)


def _slug(value: str) -> str:
    normalized = re.sub(r"[^a-z0-9]+", "-", value.lower()).strip("-")
    return normalized or "bench"


def _new_run_id(name: str, output_dir: str) -> str:
    prefix = _slug(name)[:30].rstrip("-")
    timestamp = datetime.now(timezone.utc).strftime("%Y%m%d-%H%M%S")
    root = Path(output_dir)
    for _ in range(10):
        run_id = f"{prefix}-{timestamp}-{secrets.token_hex(3)}"
        if not (root / run_id).exists():
            return run_id
    raise RuntimeError("could not allocate a unique benchmark run ID")


def _run(command: RunCommand) -> dict[str, object]:
    run_id = validate_run_id(command.run_id) if command.run_id else _new_run_id(command.name, command.output_dir)
    if command.is_managed:
        command.bench_config(
            base_url="http://frontend.invalid/v1",
            model=command.model or "deployed-model",
            run_id=run_id,
            execution_context="managed",
        ).validate()
        logger.info("Benchmark run ID: %s", run_id)
        return run_managed(command, run_id)

    config = command.bench_config(
        base_url=command.base_url,
        model=command.model,
        run_id=run_id,
        execution_context="endpoint",
    )
    logger.info("Benchmark run ID: %s", run_id)
    logger.info("%s", config.summary())
    manifest_path = Path(command.output_dir) / run_id / "manifest.json"
    try:
        result = asyncio.run(select_runner(config).run())
    except Exception:
        write_json_atomic(
            manifest_path,
            {
                "run_id": run_id,
                "execution_context": "endpoint",
                "resources_owned": False,
                "phase": "failed",
            },
        )
        raise
    write_json_atomic(
        manifest_path,
        {
            "run_id": run_id,
            "execution_context": "endpoint",
            "resources_owned": False,
            "phase": "completed",
        },
    )
    result["run_id"] = run_id
    return result


def main(argv: Sequence[str] | None = None) -> int:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s [%(name)s] %(message)s")
    logging.getLogger("httpx").setLevel(logging.WARNING)
    logging.getLogger("openai").setLevel(logging.WARNING)
    command = parse_arguments(argv)
    try:
        if isinstance(command, CleanupCommand):
            cleanup_managed(command)
            logger.info("Cleaned benchmark run: %s", command.run_id)
            return 0
        result = _run(command)
        logger.info("Benchmark complete: %s (%s)", result["run_id"], result["output_dir"])
        return 0
    except (OSError, RuntimeError, ValueError) as exc:
        logger.error("%s", exc)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
