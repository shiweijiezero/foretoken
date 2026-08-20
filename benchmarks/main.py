# SPDX-License-Identifier: Apache-2.0
# SPDX-FileCopyrightText: Copyright contributors to the Foretoken project


"""Foretoken benchmark CLI entry point."""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Sequence
from dataclasses import replace

from benchmarks.arguments import parse_arguments
from benchmarks.deployment import DeploymentError, discover_endpoint
from benchmarks.logger.cli import configure_logging, print_endpoint
from benchmarks.runner.select_runner import select_runner

logger = logging.getLogger(__name__)


def main(argv: Sequence[str] | None = None) -> None:
    """Run a benchmark against a deployment or existing endpoint."""
    configure_logging()
    try:
        command = parse_arguments(argv)
        config = command.config
        if command.deployment:
            endpoint = discover_endpoint(
                command.deployment,
                command.wait_timeout,
                requested_model=config.endpoint.model,
                api_key=config.endpoint.api_key,
            )
            config.endpoint = replace(
                config.endpoint,
                url=endpoint.url,
                model=endpoint.model,
                headers=endpoint.headers,
            )
            print_endpoint(endpoint.url, endpoint.models, endpoint.hostname)
            if not config.dataset.prompt and not config.dataset.dataset:
                config.dataset = replace(config.dataset, prompt="Hello")

        config.validate()
        logger.info("%s", config.summary())
        result = asyncio.run(select_runner(config).run())
        if result["metrics"]["success_num"] == 0:
            raise SystemExit(1)
    except (DeploymentError, ValueError) as exc:
        raise SystemExit(str(exc)) from exc


if __name__ == "__main__":
    main()
