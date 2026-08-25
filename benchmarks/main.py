# SPDX-License-Identifier: Apache-2.0
# SPDX-FileCopyrightText: Copyright contributors to the Foretoken project


"""Foretoken benchmark CLI entry point."""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Sequence
from contextlib import nullcontext
from dataclasses import replace

from benchmarks.arguments import parse_arguments
from benchmarks.deployment import DeploymentError, benchmark_deployment
from benchmarks.logger.cli import configure_logging, print_endpoint
from benchmarks.runner.select_runner import select_runner

logger = logging.getLogger(__name__)


def main(argv: Sequence[str] | None = None) -> None:
    """Run a benchmark against a deployment or existing endpoint."""
    try:
        command = parse_arguments(argv)
        config = command.config
        if (
            command.deploy
            and not config.dataset.prompt
            and not config.dataset.dataset
        ):
            config.dataset = replace(config.dataset, prompt="Hello")
        config.validate()
        configure_logging(not config.output.includes("quiet"))
        service_context = (
            benchmark_deployment(
                command.deploy,
                command.wait_timeout,
                requested_model=config.endpoint.model,
                api_key=config.endpoint.api_key,
            )
            if command.deploy
            else nullcontext(None)
        )
        with service_context as endpoint:
            if endpoint is not None:
                config.endpoint = replace(
                    config.endpoint,
                    url=endpoint.url,
                    model=endpoint.model,
                    headers=endpoint.headers,
                )
                if not config.output.includes("quiet"):
                    print_endpoint(endpoint.url, endpoint.models, endpoint.hostname)

            logger.info("%s", config.summary())
            result = asyncio.run(select_runner(config).run())
            if result["metrics"]["success_num"] == 0:
                raise SystemExit(1)
    except (DeploymentError, ValueError) as exc:
        raise SystemExit(str(exc)) from exc


if __name__ == "__main__":
    main()
