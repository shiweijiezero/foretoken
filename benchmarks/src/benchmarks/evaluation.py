# SPDX-License-Identifier: Apache-2.0
# SPDX-FileCopyrightText: Copyright contributors to the Foretoken project

"""Dispatch answer scoring or distribution comparisons through shared service and result lifecycles."""

from __future__ import annotations

import sqlite3
import sys
from collections.abc import Sequence

import wandb
from foretoken.diagnostics import configure_logging
from foretoken.errors import DeploymentError

from benchmarks.config.evaluation import (
    EvaluationConfig,
    parse_evaluation_arguments,
)
from benchmarks.model_service import ModelServiceResolver
from benchmarks.results.experiment import experiment_output
from benchmarks.results.output import ResultOutputs
from benchmarks.runs.evaluation import run_evaluation, run_evaluation_comparison


def main(argv: Sequence[str] | None = None) -> None:
    """Select evaluation and retain one experiment record across service preparation and execution."""
    arguments = tuple(sys.argv[1:] if argv is None else argv)
    try:
        config, help_requested = parse_evaluation_arguments(arguments)
        if config.evaluator is not None:
            if config.evaluator == "lm-eval":
                from benchmarks.integrations.lm_eval.cli import validate_arguments
            else:
                from benchmarks.integrations.evalscope.cli import validate_arguments

            validate_arguments(["--help"] if help_requested else list(config.arguments))
        if help_requested:
            return
        # Framework imports may configure root logging; the CLI owns presentation
        # before experiment and run scopes attach their recording handlers.
        configure_logging(not config.outputs.includes("quiet"))
        with experiment_output(config, "eval", arguments) as config:
            _run(config)
    except (DeploymentError, OSError, sqlite3.Error, ValueError, wandb.errors.Error) as error:
        raise SystemExit(str(error)) from error


def _run(config: EvaluationConfig) -> None:
    """Execute the selected evaluation adapter with validated native options."""
    if config.evaluator is None:
        from benchmarks.config.distribution_comparison import (
            parse_distribution_comparison_arguments,
        )
        from benchmarks.runs.distribution_comparison import (
            run_distribution_comparison,
            run_greedy_comparison,
        )

        comparison = parse_distribution_comparison_arguments(config.arguments, config.services)
        if comparison.greedy_compare:
            run_greedy_comparison(config, comparison)
        else:
            run_distribution_comparison(config, comparison)
        return
    if len(config.services) > 1:
        run_evaluation_comparison(config)
    else:
        with (
            ResultOutputs(config, None, directory_prefix="eval-") as outputs,
            ModelServiceResolver().resolve(config.service) as service,
        ):
            run = run_evaluation(config, service, outputs=outputs)
            if run.exit_code:
                raise SystemExit(run.exit_code if run.exit_code > 0 else 128 - run.exit_code)
