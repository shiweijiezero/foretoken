# SPDX-License-Identifier: Apache-2.0
# SPDX-FileCopyrightText: Copyright contributors to the Foretoken project

"""Native evaluator entry points isolated from the serving and publication process."""

from __future__ import annotations

import argparse
from dataclasses import asdict
import json
import os
from pathlib import Path
import shutil
import sys
from typing import Any


def native_arguments(evaluator: str, arguments: list[str]) -> argparse.Namespace:
    """Use the installed evaluator's parser, including aliases and structured native options."""
    parser = argparse.ArgumentParser(
        prog=f"foretoken eval --evaluator {evaluator}", allow_abbrev=False
    )
    if evaluator == "lm-eval":
        from lm_eval._cli.run import Run

        commands = parser.add_subparsers()
        Run.create(commands)
        task_parser = commands.choices["run"]
        task_parser.allow_abbrev = False
        if arguments == ["--help"]:
            task_parser.prog = parser.prog
            task_parser.usage = "%(prog)s [PATH | --url URL] [options]"
            task_parser.epilog = "Connection and result options are listed above; remaining options use native lm-eval syntax."
            for action in task_parser._actions:
                if action.dest in {
                    "model",
                    "output_path",
                    "wandb_args",
                    "wandb_config_args",
                }:
                    action.help = argparse.SUPPRESS
        return parser.parse_args(["run", *arguments])
    from evalscope.arguments import add_argument

    add_argument(parser)
    if arguments == ["--help"]:
        for action in parser._actions:
            if action.dest in {
                "model",
                "api_url",
                "api_key",
                "work_dir",
                "eval_type",
                "eval_backend",
            }:
                action.help = argparse.SUPPRESS
    return parser.parse_args(arguments)


def validate_model_transport(arguments: dict[str, Any]) -> None:
    """Reject service credentials in native options before upstream logs or persists them."""
    for field in ("api_key", "auth_token"):
        if arguments.get(field):
            raise ValueError(
                f"Model argument {field} is reserved; supply authentication using `--api-key`"
            )
    for field in ("header", "default_headers"):
        headers = arguments.get(field) or {}
        if any(
            name.lower() in ("authorization", "proxy-authorization") for name in headers
        ):
            raise ValueError(
                "Authorization headers are reserved; supply authentication using `--api-key`"
            )


def _run_lm_eval(arguments: list[str], service: dict[str, Any], directory: str) -> None:
    """Use the harness CLI runner with a chat transport that owns service authentication."""
    from lm_eval.api.registry import register_model
    from lm_eval.models.openai_completions import LocalChatCompletion
    from lm_eval.utils import setup_logging

    @register_model("foretoken-chat-completions")
    class ForetokenChatCompletion(LocalChatCompletion):
        """Keep routing and credentials out of persisted harness model arguments."""

        @property
        def api_key(self) -> str:
            return service["api_key"]

        @property
        def header(self) -> dict[str, str]:
            return {
                **super().header,
                **service["headers"],
                "Authorization": f"Bearer {self.api_key}",
            }

    from lm_eval.config.evaluate_config import EvaluatorConfig

    setup_logging()
    args = native_arguments("lm-eval", arguments)
    args.output_path = directory
    if not hasattr(args, "apply_chat_template"):
        args.apply_chat_template = True
    # Resolve YAML and native CLI precedence upstream before injecting the service.
    # Passing a new --model_args string would discard model options loaded from YAML.
    if args.config:
        from lm_eval.utils import simple_parse_args_string

        configured = (
            EvaluatorConfig.load_yaml_config(args.config).get("model_args") or {}
        )
        validate_model_transport(
            simple_parse_args_string(configured)
            if isinstance(configured, str)
            else configured
        )
    validate_model_transport(args.model_args or {})
    config = EvaluatorConfig.from_cli(args)
    if config.wandb_args:
        raise ValueError(
            "Use --output wandb and --wandb-project instead of native wandb_args"
        )
    config.config = None
    config.model = "foretoken-chat-completions"
    config.model_args.update(model=service["model"], base_url=service["chat_url"])
    for key in ("api_key", "auth_token"):
        config.model_args.pop(key, None)
    config.output_path = directory
    config.apply_chat_template = config.apply_chat_template or True
    args.func(argparse.Namespace(**asdict(config)))


def _run_evalscope(
    arguments: list[str], service: dict[str, Any], directory: str
) -> None:
    """Use EvalScope's native configuration and runner for the selected HTTP service."""
    from evalscope.config import parse_task_config
    from evalscope.run import run_task

    args = native_arguments(
        "evalscope",
        [
            *arguments,
            "--model",
            service["model"],
            "--api-url",
            service["api_root"],
            "--eval-type",
            "openai_api",
            "--work-dir",
            directory,
            "--no-timestamp",
        ],
    )
    config = parse_task_config(args)
    validate_model_transport(config.model_args)
    config.model = service["model"]
    config.api_url = service["api_root"]
    config.eval_type = "openai_api"
    config.work_dir = directory
    config.no_timestamp = True
    # EvalScope's OpenAI client reads this environment value when api_key is unset.
    # The child owns its environment, so other invocations cannot inherit credentials.
    os.environ["EVALSCOPE_API_KEY"] = service["api_key"]
    config.api_key = None
    headers = dict(config.model_args.get("default_headers") or {})
    headers.update(service["headers"])
    if headers:
        config.model_args["default_headers"] = headers
    reports = run_task(config)
    # A resumed run writes into its cache. Export only reports returned by this
    # invocation, not older tasks or models that happen to share that directory.
    if Path(config.work_dir).resolve() != Path(directory).resolve():
        for task, report in reports.items():
            if not report:
                continue
            destination = Path(directory) / "reports" / report.model_name
            destination.mkdir(parents=True, exist_ok=True)
            (destination / f"{task}.json").write_text(
                json.dumps(report.to_dict(), ensure_ascii=False, indent=2),
                encoding="utf-8",
            )
            subsets = {
                subset.name
                for metric in report.metrics
                for category in metric.categories
                for subset in category.subsets
            }
            for kind in ("predictions", "reviews"):
                for subset in subsets:
                    relative = Path(kind) / report.model_name / f"{task}_{subset}.jsonl"
                    source = Path(config.work_dir) / relative
                    if source.exists():
                        target = Path(directory) / relative
                        target.parent.mkdir(parents=True, exist_ok=True)
                        shutil.copy2(source, target)


def main() -> None:
    """Receive one invocation through stdin so service credentials never enter argv."""
    invocation = json.load(sys.stdin)
    runner = _run_lm_eval if invocation["evaluator"] == "lm-eval" else _run_evalscope
    runner(invocation["arguments"], invocation["service"], invocation["directory"])


if __name__ == "__main__":
    main()
