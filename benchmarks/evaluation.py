# SPDX-License-Identifier: Apache-2.0
# SPDX-FileCopyrightText: Copyright contributors to the Foretoken project

"""Compose native quality evaluation with Foretoken service discovery and result destinations."""

from __future__ import annotations

from collections.abc import Sequence
import sys

from benchmarks.config.evaluation import parse_evaluation_arguments
from benchmarks.integrations.quality import native_arguments


def main(argv: Sequence[str] | None = None) -> None:
    """Parse native task options before resolving a service and running the selected evaluator."""
    try:
        config, help_requested = parse_evaluation_arguments(
            sys.argv[1:] if argv is None else argv
        )
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

        from benchmarks.model_service import resolve_model_service
        from benchmarks.results.console import configure_logging
        from benchmarks.runs.evaluation import run_evaluation

        configure_logging(not config.outputs.includes("quiet"))
        with resolve_model_service(config.service) as service:
            run_evaluation(config, service)
    except ValueError as error:
        raise SystemExit(str(error)) from error
