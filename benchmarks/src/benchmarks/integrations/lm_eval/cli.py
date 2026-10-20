# SPDX-License-Identifier: Apache-2.0
# SPDX-FileCopyrightText: Copyright contributors to the Foretoken project

"""Parse native harness options while reserving service and publication ownership."""

from __future__ import annotations

import argparse


def parse_arguments(arguments: list[str]) -> argparse.Namespace:
    """Return native CLI options for the harness runner, loading the framework only when selected."""
    from lm_eval._cli.run import Run

    parser = argparse.ArgumentParser(
        prog="foretoken eval --evaluator lm-eval", allow_abbrev=False
    )
    commands = parser.add_subparsers()
    Run.create(commands)
    task_parser = commands.choices["run"]
    task_parser.allow_abbrev = False
    if arguments == ["--help"]:
        task_parser.prog = parser.prog
        task_parser.usage = "%(prog)s [PATH | --url URL] [options]"
        task_parser.epilog = "Connection and result options are listed above; remaining options use native lm-eval syntax."
        for action in task_parser._actions:
            if action.dest in {"model", "output_path", "wandb_args", "wandb_config_args"}:
                action.help = argparse.SUPPRESS
    return parser.parse_args(["run", *arguments])


def validate_arguments(arguments: list[str]) -> None:
    """Validate harness options before service acquisition; native help exits through its parser."""
    native = parse_arguments(arguments)
    for name in ("model", "output_path", "wandb_args"):
        if getattr(native, name, None):
            raise ValueError(
                f"Native {name} is managed by Foretoken; use --model, --url, --output-dir or --output instead"
            )
    for name in ("model", "base_url", "api_key", "auth_token"):
        if name in (native.model_args or {}):
            raise ValueError(
                f"model arguments cannot replace {name}; select the service with --model, --url and --api-key"
            )
