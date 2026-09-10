# SPDX-License-Identifier: Apache-2.0
# SPDX-FileCopyrightText: Copyright contributors to the Foretoken project


"""Foretoken benchmark CLI entry point."""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Sequence
from contextlib import nullcontext
from dataclasses import replace

from foretoken.kubernetes import Kubectl, timeout_seconds
from foretoken.manifest import DeploymentError
from foretoken.profiling import benchmark_profile

from benchmarks.arguments import parse_arguments
from benchmarks.deployment import benchmark_deployment
from benchmarks.logger.cli import configure_logging, print_endpoint
from benchmarks.runner.select_runner import select_runner

logger = logging.getLogger(__name__)


def main(argv: Sequence[str] | None = None) -> None:
    """Run a benchmark against a deployment or existing endpoint."""
    try:
        command = parse_arguments(argv)
        config = command.config
        if (
            command.kustomize_path
            and not config.dataset.prompt
            and not config.dataset.dataset
        ):
            config.dataset = replace(config.dataset, prompt="Hello")
        config.validate()
        if config.param_sweep.bench_params and not command.kustomize_path:
            raise ValueError(
                "--bench-params requires a Foretoken Kustomize deployment"
            )
        configure_logging(not config.output.includes("quiet"))
        if command.kustomize_path:
            service_context = benchmark_deployment(
                command.kustomize_path,
                command.wait_timeout,
                requested_model=config.endpoint.model,
                api_key=config.endpoint.api_key,
            )
        else:
            service_context = nullcontext(None)
        with service_context as endpoint:
            if endpoint is not None:
                config.endpoint = replace(
                    config.endpoint,
                    url=endpoint.url,
                    model=endpoint.model,
                    headers=endpoint.headers,
                )
                config.output = replace(
                    config.output,
                    gpu_count=endpoint.gpu_count,
                )
                if not config.output.includes("quiet"):
                    print_endpoint(endpoint.url, endpoint.models, endpoint.hostname)

            logger.info("%s", config.summary())
            runner = select_runner(config)
            if config.profiling.profiler:
                if endpoint is None:
                    raise ValueError("--profile requires a running deployment")
                profile_context = benchmark_profile(
                    endpoint.namespace,
                    endpoint.model_services,
                    Kubectl(),
                    timeout_seconds(command.wait_timeout),
                    config.output.output_dir,
                    config.profiling.window,
                )
            else:
                profile_context = nullcontext(None)
            with profile_context as profile:
                if profile is not None:
                    runner.before_requests = profile.start
                result = asyncio.run(runner.run())
            if result["metrics"]["success_num"] == 0:
                raise SystemExit(1)
    except (DeploymentError, ValueError) as exc:
        raise SystemExit(str(exc)) from exc


if __name__ == "__main__":
    main()
