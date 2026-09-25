# SPDX-License-Identifier: Apache-2.0
# SPDX-FileCopyrightText: Copyright contributors to the Foretoken project

"""Native evaluator entry points isolated from the serving and publication process."""

from __future__ import annotations

import argparse
from contextlib import closing, nullcontext
from dataclasses import asdict
import json
import os
from pathlib import Path
import shutil
import sqlite3
import sys
from typing import Any

from benchmarks.integrations.lm_eval_responses import LmEvalResponses


def native_arguments(evaluator: str, arguments: list[str]) -> argparse.Namespace:
    """Use the installed evaluator's parser, including aliases and structured native options."""
    parser = argparse.ArgumentParser(
        prog=f"foretoken eval --evaluator {evaluator}", allow_abbrev=False
    )
    # Each framework initializes process-wide registries and logging on import.
    # Load only the selected evaluator in the parent and its execution child.
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


def _run_lm_eval(
    arguments: list[str], service: dict[str, Any], directory: str, *, resume: bool = False
) -> None:
    """Use the harness CLI runner with a chat transport that owns service authentication."""
    from lm_eval.api.registry import register_model
    from lm_eval.config.evaluate_config import EvaluatorConfig
    from lm_eval.models.openai_completions import LocalChatCompletion
    from lm_eval.models.api_models import LMEVAL_MODEL_NONE_ANSWER_PLACEHOLDER
    from lm_eval.utils import setup_logging, simple_parse_args_string

    @register_model("foretoken-chat-completions")
    class ForetokenChatCompletion(LocalChatCompletion):
        """Keep routing and credentials out of persisted harness model arguments."""

        def generate_until(self, requests, disable_tqdm: bool = False):
            """Reuse individual completed generations and let the native adapter produce missing draws."""
            if responses is None:
                return super().generate_until(requests, disable_tqdm=disable_tqdm)
            if any(len(request.args) != 2 for request in requests):
                if resume:
                    raise ValueError("lm-eval resume supports text generation tasks")
                return super().generate_until(requests, disable_tqdm=disable_tqdm)
            slots = [responses.reserve(request.args) for request in requests]
            missing = [request for request, slot in zip(requests, slots) if responses.get(slot) is None]
            if missing:
                previous_hook = self.cache_hook
                self.set_cache_hook(responses)
                try:
                    super().generate_until(missing, disable_tqdm=disable_tqdm)
                finally:
                    self.set_cache_hook(previous_hook)
            return [responses.get(slot) for slot in slots]

        async def get_batched_requests(self, requests, cache_keys, **kwargs):
            """Bind concurrent callbacks before upstream dispatch so retries keep the same sample slot."""
            if responses is not None and self.cache_hook is responses:
                cache_keys = [responses.claim(key) for key in cache_keys]
            return await super().get_batched_requests(requests, cache_keys, **kwargs)

        def parse_generations(self, outputs, **kwargs):
            """Use the upstream null-answer value before either sync or async completion callbacks."""
            values = super().parse_generations(outputs, **kwargs)
            if responses is None:
                return values
            return [LMEVAL_MODEL_NONE_ANSWER_PLACEHOLDER if value is None else value for value in values]

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

    setup_logging()
    args = native_arguments("lm-eval", arguments)
    args.output_path = directory
    if not hasattr(args, "apply_chat_template"):
        args.apply_chat_template = True
    # Resolve YAML and native CLI precedence upstream before injecting the service.
    # Passing a new --model_args string would discard model options loaded from YAML.
    if args.config:
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
    if resume and (args.use_cache is not None or config.use_cache is not None):
        raise ValueError("Use --resume or native --use_cache, not both")
    config.config = None
    config.model = "foretoken-chat-completions"
    config.model_args.update(model=service["model"], base_url=service["chat_url"])
    for key in ("api_key", "auth_token"):
        config.model_args.pop(key, None)
    config.output_path = directory
    config.apply_chat_template = config.apply_chat_template or True
    # Completion records stay local, outside the native report directory uploaded to W&B.
    progress = LmEvalResponses(Path(directory).parent) if config.use_cache is None else nullcontext(None)
    with progress as responses:
        args.func(argparse.Namespace(**asdict(config)))


def _run_evalscope(
    arguments: list[str], service: dict[str, Any], directory: str, *, resume: bool = False
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
    if resume:
        if config.use_cache is not None:
            raise ValueError("Use --resume or native --use-cache, not both")
        config.use_cache = directory
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


def _restore_native_cache(evaluator: str, model: str, source: str, native: Path) -> None:
    """Copy native progress into this invocation without modifying the previous evaluation."""
    previous_directory = Path(source).expanduser().resolve()
    previous = json.loads((previous_directory / "config.json").read_text(encoding="utf-8"))
    if previous.get("mode") != "evaluation" or previous.get("evaluator") != evaluator:
        raise ValueError("--resume requires a quality evaluation using the same evaluator")
    if previous.get("model") != model:
        raise ValueError("--resume requires the same served model as the previous evaluation")
    cache = previous_directory / "native"
    if evaluator == "lm-eval":
        database = previous_directory / LmEvalResponses.filename
        if not database.is_file():
            raise ValueError("The previous lm-eval run has no saved generation progress")
        with closing(sqlite3.connect(database.as_uri() + "?mode=ro", uri=True)) as src:
            with closing(sqlite3.connect(native.parent / LmEvalResponses.filename)) as dst:
                src.backup(dst)
    else:
        if not (cache / "configs" / "task_config.yaml").is_file():
            raise ValueError("The previous EvalScope run has no resumable task configuration")
        for name in ("configs", "predictions", "reviews"):
            if (cache / name).is_dir():
                shutil.copytree(cache / name, native / name)


def main() -> None:
    """Receive one invocation through stdin so service credentials never enter argv."""
    invocation = json.load(sys.stdin)
    if invocation["resume"]:
        _restore_native_cache(
            invocation["evaluator"], invocation["service"]["model"],
            invocation["resume"], Path(invocation["directory"]),
        )
    runner = _run_lm_eval if invocation["evaluator"] == "lm-eval" else _run_evalscope
    runner(
        invocation["arguments"], invocation["service"], invocation["directory"],
        resume=bool(invocation["resume"]),
    )


if __name__ == "__main__":
    main()
