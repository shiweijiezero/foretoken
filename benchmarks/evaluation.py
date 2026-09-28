# SPDX-License-Identifier: Apache-2.0
# SPDX-FileCopyrightText: Copyright contributors to the Foretoken project

"""Dispatch text scoring, video scoring, or model distribution comparisons."""

from __future__ import annotations

from collections.abc import Sequence
import sys

from benchmarks.config.evaluation import native_arguments, parse_evaluation_arguments
from benchmarks.config.video_evaluation import (
    parse_vbench_evaluation_arguments,
    parse_vbench_setup_arguments,
)
from benchmarks.integrations.vbench import VBenchEvaluator
from benchmarks.model_service import resolve_model_service
from benchmarks.results.console import configure_logging
from benchmarks.runs.evaluation import run_evaluation
from benchmarks.runs.video_evaluation import run_video_evaluation


def main(argv: Sequence[str] | None = None) -> None:
    """Route video artifacts or resolve services for text scoring and comparisons."""
    try:
        arguments = tuple(sys.argv[1:] if argv is None else argv)
        if arguments[:1] == ("setup",):
            from benchmarks.integrations.vbench_setup import setup_vbench

            setup_vbench(parse_vbench_setup_arguments(arguments[1:]))
            return
        if any(argument.partition("=")[0] == "--video" for argument in arguments):
            config = parse_vbench_evaluation_arguments(arguments)
            configure_logging(not config.outputs.includes("quiet"))
            run_video_evaluation(config, VBenchEvaluator(config))
            return
        config, help_requested = parse_evaluation_arguments(
            arguments
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
