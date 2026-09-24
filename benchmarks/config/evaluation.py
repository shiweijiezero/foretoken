# SPDX-License-Identifier: Apache-2.0
# SPDX-FileCopyrightText: Copyright contributors to the Foretoken project

"""Service and result options surrounding an upstream quality evaluation."""

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
    """Keep service discovery and publication separate from native evaluator options."""

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
    # PATH is the first operand. A native option's value must never become PATH.
    path = arguments.pop(0) if arguments and not arguments[0].startswith("-") else ""
    source = ModelServiceSource()
    output = BenchmarkOutputConfig()
    tracking = WandbRunConfig()
    parser = argparse.ArgumentParser(
        prog="foretoken eval",
        allow_abbrev=False,
        add_help=False,
        usage="%(prog)s [PATH | --url URL] [options] [evaluator options]",
        description="Score a model service with lm-evaluation-harness or EvalScope.",
        epilog="PATH is a Kustomize directory placed immediately after eval. Native task options need no separator.",
    )
    parser.add_argument(
        "-h",
        "--help",
        action="store_true",
        help="show Foretoken and selected evaluator options",
    )
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
        parser.print_help()
    else:
        config.service.validate()
        config.outputs.validate()
    return config, options.help
