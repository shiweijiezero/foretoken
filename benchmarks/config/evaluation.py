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
    """Share service and output options; evaluator is absent for reference comparisons."""

    service: ModelServiceSource
    evaluator: str | None
    arguments: tuple[str, ...]
    outputs: BenchmarkOutputConfig
    wandb: WandbRunConfig
    resume: str = ""

    def to_dict(self) -> dict:
        """Return publication settings without authentication or native secret-bearing arguments."""
        return {
            **({"evaluator": self.evaluator} if self.evaluator is not None else {}),
            **({"model": self.service.model} if self.service.model else {}),
            "output": {"destinations": self.outputs.destinations},
        }


def parse_evaluation_arguments(argv: Sequence[str]) -> tuple[EvaluationConfig, bool]:
    """Extract exact Foretoken options; leave task options and their values in original order."""
    arguments = list(argv)
    comparison = any(
        argument.partition("=")[0] in ("--reference", "--reference-url", "--reference-model")
        for argument in arguments
    )
    # PATH is the first operand. A native option's value must never become PATH.
    path = arguments.pop(0) if arguments and not arguments[0].startswith("-") else ""
    source = ModelServiceSource()
    output = BenchmarkOutputConfig()
    tracking = WandbRunConfig()
    parser = argparse.ArgumentParser(
        prog="foretoken eval",
        allow_abbrev=False,
        add_help=False,
        usage="%(prog)s [PATH | --url URL] [options]",
        description="Compare model output distributions." if comparison else "Evaluate model quality with an evaluation framework.",
        epilog=(
            "PATH selects the candidate deployment; --reference selects its reference."
            if comparison else
            "Add --reference PATH to compare model distributions. "
            "PATH is a Kustomize directory. Native task options need no separator."
        ),
    )
    parser.add_argument(
        "-h",
        "--help",
        action="store_true",
        help="show comparison options" if comparison else "show Foretoken and selected evaluator options",
    )
    parser.add_argument(
        "--evaluator", choices=("lm-eval", "evalscope"), default=None,
        help="evaluation framework (default: lm-eval); omit with a reference",
    )
    parser.add_argument(
        "--resume", default="", metavar="RESULT_DIR",
        help="reuse completed evaluation work from a previous result directory; repeat the original task options",
    )
    parser.add_argument(
        "--url", default=source.url, help="existing Chat Completions or Completions URL"
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
    if comparison and options.evaluator is not None:
        parser.error("--evaluator cannot be combined with a reference")
    config = EvaluationConfig(
        service=ModelServiceSource(
            kustomize_path=path,
            url=options.url,
            model=options.model,
            api_key=options.api_key,
            wait_timeout=options.wait_timeout,
        ),
        evaluator=None if comparison else options.evaluator or "lm-eval",
        resume=options.resume,
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
            from benchmarks.config.distribution_comparison import add_distribution_comparison_arguments

            add_distribution_comparison_arguments(parser)
        parser.print_help()
    else:
        if not comparison:
            config.service.validate()
        config.outputs.validate()
    return config, options.help
