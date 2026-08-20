# SPDX-License-Identifier: Apache-2.0
# SPDX-FileCopyrightText: Copyright contributors to the Foretoken project


"""Foretoken benchmark CLI entry point."""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Sequence
from dataclasses import replace

from benchmarks.arguments import BenchCommand, parse_arguments
from benchmarks.console import configure_logging, print_target
from benchmarks.deployment import DeploymentError, benchmark_deployment
from benchmarks.runner.select_runner import select_runner

logger = logging.getLogger(__name__)


def _run_benchmark(command: BenchCommand) -> None:
    config = command.config
    if command.deployment:
        resources, endpoint, model = benchmark_deployment(
            command.deployment,
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
        print_target(endpoint.url, endpoint.models, resources.hostname)

    config.validate()
    logger.info("%s", config.summary())
    result = asyncio.run(select_runner(config).run())
    if result["metrics"]["success_num"] == 0:
        raise SystemExit(1)


def main(argv: Sequence[str] | None = None) -> None:
    """Run a benchmark against a deployment or existing endpoint."""
    configure_logging()
    try:
        _run_benchmark(parse_arguments(argv))
    except (DeploymentError, ValueError) as exc:
        raise SystemExit(str(exc)) from exc


if __name__ == "__main__":
    main()
