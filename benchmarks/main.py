# SPDX-License-Identifier: Apache-2.0
# SPDX-FileCopyrightText: Copyright contributors to the Foretoken project


"""Foretoken benchmark CLI entry point."""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Sequence
from dataclasses import replace

from benchmarks.arguments import BenchCommand, parse_arguments
from benchmarks.project import ProjectError, benchmark_project
from benchmarks.runner.select_runner import select_runner

logger = logging.getLogger(__name__)


def _configure_logging() -> None:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s [%(name)s] %(message)s",
    )
    # Keep request chatter off the console so tqdm can refresh one bar in place.
    logging.getLogger("httpx").setLevel(logging.WARNING)
    logging.getLogger("httpcore").setLevel(logging.WARNING)


def _print_project(endpoint_url: str, models: tuple[str, ...], hostname: str) -> None:
    print(f"Endpoint: {endpoint_url}")
    if hostname:
        print(f"Hostname: {hostname}")
    print(f"Models: {', '.join(models)}")


def _run_benchmark(command: BenchCommand) -> None:
    config = command.config
    if command.project:
        resources, endpoint, model = benchmark_project(
            command.project,
            command.wait_timeout,
            requested_model=config.target.model,
            api_key=config.target.api_key,
        )
        config.target = replace(
            config.target,
            url=endpoint.url,
            model=model,
            headers=endpoint.headers,
        )
        _print_project(endpoint.url, endpoint.models, resources.hostname)

    config.validate()
    logger.info("%s", config.summary())
    result = asyncio.run(select_runner(config).run())
    if result["metrics"]["success_num"] == 0:
        raise SystemExit(1)


def main(argv: Sequence[str] | None = None) -> None:
    """Run a benchmark against a project or existing endpoint."""
    _configure_logging()
    try:
        _run_benchmark(parse_arguments(argv))
    except (ProjectError, ValueError) as exc:
        raise SystemExit(str(exc)) from exc


if __name__ == "__main__":
    main()
