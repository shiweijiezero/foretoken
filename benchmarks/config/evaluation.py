# SPDX-License-Identifier: Apache-2.0
# SPDX-FileCopyrightText: Copyright contributors to the Foretoken project

"""Shared service and result options for answer scoring and model comparisons."""

from __future__ import annotations

import argparse
from collections.abc import Sequence
from dataclasses import dataclass

from benchmarks.config.benchmark import (
    BenchmarkOutputConfig,
    ModelServiceSource,
    WandbRunConfig,
)


@dataclass
class EvaluationConfig:
    """Keep service discovery and publication separate from task-specific options."""

    service: ModelServiceSource
    evaluator: str
    arguments: tuple[str, ...]
    outputs: BenchmarkOutputConfig
    wandb: WandbRunConfig

    def to_dict(self) -> dict:
        """Return publication settings without authentication or native secret-bearing arguments."""
        return {
            "evaluator": self.evaluator,
            "model": self.service.model,
            "output": {"destinations": self.outputs.destinations},
        }


def parse_evaluation_arguments(argv: Sequence[str]) -> tuple[EvaluationConfig, bool]:
    """Extract exact Foretoken options; leave task options and their values in original order."""
    arguments = list(argv)
    comparison = arguments[:1] == ["compare"]
    if comparison:
        arguments.pop(0)
    # PATH is the first operand. A native option's value must never become PATH.
    path = arguments.pop(0) if arguments and not arguments[0].startswith("-") else ""
    source = ModelServiceSource()
    output = BenchmarkOutputConfig()
    tracking = WandbRunConfig()
    parser = argparse.ArgumentParser(
        prog="foretoken eval compare" if comparison else "foretoken eval",
        allow_abbrev=False,
        add_help=False,
        usage="%(prog)s [PATH | --url URL] [options]",
        description="Compare model output distributions." if comparison else "Score model answers with an evaluation framework.",
        epilog=(
            "PATH selects the candidate deployment; --reference selects its reference."
            if comparison else
            "Use 'foretoken eval compare --help' to compare model distributions. "
            "PATH is a Kustomize directory. Native task options need no separator."
        ),
    )
    parser.add_argument(
        "-h",
        "--help",
        action="store_true",
        help="show comparison options" if comparison else "show Foretoken and selected evaluator options",
    )
    if comparison:
        parser.set_defaults(evaluator="compare")
    else:
        parser.add_argument(
            "--evaluator", choices=("lm-eval", "evalscope"), default="lm-eval"
        )
    parser.add_argument(
        "--url", default=source.url, help="existing Chat Completions URL"
    )
    parser.add_argument(
        "--model",
        default=source.model,
        help="served model ID; inferred for a single-model PATH",
    )
    parser.add_argument(
        "--api-key", default=source.api_key, help="model service API key"
    )
    parser.add_argument(
        "--wait-timeout",
        default=source.wait_timeout,
        help="deployment readiness timeout",
    )
    parser.add_argument(
        "--output",
        default=output.destinations,
        type=lambda value: tuple(value.split(",")),
        help="local,wandb,quiet (default: local,wandb)",
    )
    parser.add_argument(
        "--output-dir",
        default=output.output_dir,
        help="parent directory for run artifacts (default: results)",
    )
    parser.add_argument("--wandb-project", default=tracking.project)
    parser.add_argument("--wandb-entity", default=tracking.entity)
    parser.add_argument("--wandb-run-name", default=tracking.run_name)
    parser.add_argument("--wandb-group", default=tracking.group)
    options, native = parser.parse_known_args(arguments)
    config = EvaluationConfig(
        service=ModelServiceSource(
            kustomize_path=path,
            url=options.url,
            model=options.model,
            api_key=options.api_key,
            wait_timeout=options.wait_timeout,
        ),
        evaluator=options.evaluator,
        arguments=tuple(native),
        outputs=BenchmarkOutputConfig(options.output, options.output_dir),
        wandb=WandbRunConfig(
            project=options.wandb_project,
            entity=options.wandb_entity,
            run_name=options.wandb_run_name,
            group=options.wandb_group,
        ),
    )
    if options.help:
        if comparison:
            from benchmarks.config.fidelity import add_fidelity_arguments

            add_fidelity_arguments(parser)
        parser.print_help()
    else:
        if not comparison:
            config.service.validate()
        config.outputs.validate()
    return config, options.help
