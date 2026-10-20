# SPDX-License-Identifier: Apache-2.0
# SPDX-FileCopyrightText: Copyright contributors to the Foretoken project

"""Parse native EvalScope options for a Foretoken-managed HTTP service."""

from __future__ import annotations

import argparse


def parse_arguments(arguments: list[str]) -> argparse.Namespace:
    """Return native CLI options for the EvalScope runner, loading the framework only when selected."""
    from evalscope.arguments import add_argument

    parser = argparse.ArgumentParser(
        prog="foretoken eval --evaluator evalscope", allow_abbrev=False
    )
    add_argument(parser)
    if arguments == ["--help"]:
        for action in parser._actions:
            if action.dest in {"model", "api_url", "api_key", "work_dir", "eval_type", "eval_backend"}:
                action.help = argparse.SUPPRESS
    return parser.parse_args(arguments)


def validate_arguments(arguments: list[str]) -> None:
    """Validate EvalScope options before service acquisition; native help exits through its parser."""
    native = parse_arguments(arguments)
    for name in ("api_url", "work_dir"):
        if getattr(native, name, None):
            raise ValueError(
                f"Native {name} is managed by Foretoken; use --model, --url, --output-dir or --output instead"
            )
    if native.eval_type not in (None, "openai_api"):
        raise ValueError("foretoken eval uses openai_api for the selected model service")
    if native.eval_backend not in (None, "Native"):
        raise ValueError("foretoken eval uses EvalScope's Native backend for the selected model service")
    for name in ("model", "base_url", "api_key", "auth_token"):
        if name in (native.model_args or {}):
            raise ValueError(
                f"model arguments cannot replace {name}; select the service with --model, --url and --api-key"
            )
