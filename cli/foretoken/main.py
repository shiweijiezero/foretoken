# SPDX-License-Identifier: Apache-2.0
# SPDX-FileCopyrightText: Copyright contributors to the Foretoken project

"""Foretoken CLI argument parsing, command dispatch and process exit handling."""

from __future__ import annotations

import sys
from collections.abc import Sequence

from foretoken.arguments import (
    ClusterCommand,
    DeleteCommand,
    DeployCommand,
    EndpointCommand,
    EvaluationCommand,
    InstallCommand,
    PerformanceCommand,
    PlotCommand,
    ProfileViewCommand,
    StatusCommand,
    StopCommand,
    UninstallCommand,
    parse_arguments,
)
from foretoken.deployment.commands import delete, deploy, stop
from foretoken.deployment.endpoint import print_endpoint
from foretoken.deployment.status import show_status
from foretoken.diagnostics import configure_logging
from foretoken.errors import report_command_errors
from foretoken.platform.commands import install, uninstall
from foretoken.platform.local_cluster import run as run_cluster
from foretoken.profiling.viewer import view


def main(argv: Sequence[str] | None = None) -> None:
    """Dispatch parsed CLI commands and translate expected failures into process exit codes."""
    command = parse_arguments(sys.argv[1:] if argv is None else argv)
    configure_logging()
    with report_command_errors():
        if isinstance(command, ClusterCommand):
            run_cluster(command)
        elif isinstance(command, InstallCommand):
            install(command)
        elif isinstance(command, UninstallCommand):
            uninstall(command)
        elif isinstance(command, DeployCommand):
            deploy(command)
        elif isinstance(command, DeleteCommand):
            delete(command)
        elif isinstance(command, StopCommand):
            stop(command)
        elif isinstance(command, StatusCommand):
            show_status(command)
        elif isinstance(command, EndpointCommand):
            print_endpoint(command)
        elif isinstance(command, PerformanceCommand):
            # Load benchmark frameworks only for benchmark commands.
            from benchmarks.main import main as benchmark_main

            benchmark_main(command.arguments)
        elif isinstance(command, EvaluationCommand):
            from benchmarks.evaluation import main as evaluation_main

            evaluation_main(command.arguments)
        elif isinstance(command, PlotCommand):
            from benchmarks.plot import main as plot_main

            plot_main(command.arguments)
        elif isinstance(command, ProfileViewCommand):
            view(command)


if __name__ == "__main__":
    main()
