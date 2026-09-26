# SPDX-License-Identifier: Apache-2.0
# SPDX-FileCopyrightText: Copyright contributors to the Foretoken project

"""Dispatch answer scoring or distribution comparisons through shared service and result lifecycles."""

from __future__ import annotations

from collections.abc import Sequence
import sys

from benchmarks.config.evaluation import native_arguments, parse_evaluation_arguments
from benchmarks.model_service import resolve_model_service
from benchmarks.results.console import configure_logging
from benchmarks.runs.evaluation import run_evaluation


def main(argv: Sequence[str] | None = None) -> None:
    """Select distribution comparison or native answer scoring through shared service lifecycles."""
    try:
        config, help_requested = parse_evaluation_arguments(
            sys.argv[1:] if argv is None else argv
        )
        if config.evaluator is None:
            if help_requested:
                return
            from benchmarks.config.distribution_comparison import parse_distribution_comparison_arguments
            from benchmarks.runs.distribution_comparison import run_distribution_comparison

            comparison = parse_distribution_comparison_arguments(config.arguments, config.service)
            configure_logging(not config.outputs.includes("quiet"))
            run_distribution_comparison(config, comparison)
            return
        native = native_arguments(
            config.evaluator,
            ["--help"] if help_requested else list(config.arguments),
        )
        # Only transport and publication belong to Foretoken. Other native
        # options, including structured model arguments, retain upstream ownership.
        owned = (
            ("model", "output_path", "wandb_args")
            if config.evaluator == "lm-eval"
            else ("api_url", "work_dir")
        )
        for name in owned:
            value = getattr(native, name, None)
            if value:
                raise ValueError(
                    f"Native {name} is managed by Foretoken; use --model, --url, --output-dir or --output instead"
                )
        if config.evaluator == "evalscope":
            if native.eval_type not in (None, "openai_api"):
                raise ValueError(
                    "foretoken eval uses openai_api for the selected model service"
                )
            if native.eval_backend not in (None, "Native"):
                raise ValueError(
                    "foretoken eval uses EvalScope's Native backend for the selected model service"
                )
        model_args = getattr(native, "model_args", None) or {}
        for name in ("model", "base_url", "api_key", "auth_token"):
            if name in model_args:
                raise ValueError(
                    f"model arguments cannot replace {name}; select the service with --model, --url and --api-key"
                )

        configure_logging(not config.outputs.includes("quiet"))
        with resolve_model_service(config.service) as service:
            run_evaluation(config, service)
    except ValueError as error:
        raise SystemExit(str(error)) from error
