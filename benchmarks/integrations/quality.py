# SPDX-License-Identifier: Apache-2.0
# SPDX-FileCopyrightText: Copyright contributors to the Foretoken project

"""Native evaluator entry points isolated from the serving and publication process."""

from __future__ import annotations

import argparse
from contextlib import closing, nullcontext
from dataclasses import replace
import json
import logging
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
    """Run one native task set, dispatching generation and likelihood to their service APIs."""
    from functools import cached_property, partial

    from lm_eval.config.evaluate_config import EvaluatorConfig
    from lm_eval.models.openai_completions import LocalChatCompletion, LocalCompletionsAPI
    from lm_eval.models.api_models import JsonChatStr, LMEVAL_MODEL_NONE_ANSWER_PLACEHOLDER
    from lm_eval.utils import setup_logging, simple_parse_args_string

    setup_logging()
    args = native_arguments("lm-eval", arguments)
    args.output_path = directory
    if args.config:
        configured = EvaluatorConfig.load_yaml_config(args.config).get("model_args") or {}
        validate_model_transport(
            simple_parse_args_string(configured) if isinstance(configured, str) else configured
        )
    validate_model_transport(args.model_args or {})
    config = EvaluatorConfig.from_cli(args)
    if config.wandb_args:
        raise ValueError("Use --output wandb and --wandb-project instead of native wandb_args")
    if resume and (args.use_cache is not None or config.use_cache is not None):
        raise ValueError("Use --resume or native --use_cache, not both")
    chat_url = service["chat_url"]
    if chat_url.rstrip("/").endswith("/completions"):
        chat_url = f"{service['api_root']}/chat/completions"
    config.model_args.update(model=service["model"], base_url=chat_url)
    config.output_path = directory

    class ServiceAuthentication:
        """Keep credentials and Gateway routing out of persisted harness arguments."""

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

    class LikelihoodCompletion(ServiceAuthentication, LocalCompletionsAPI):
        """Use native likelihood scoring after checking that the endpoint returned a complete echo."""

        def parse_logprobs(self, outputs, tokens=None, ctxlens=None, **kwargs):
            """Reject truncated echoes before upstream slicing could report a false zero likelihood."""
            batches = [outputs] if isinstance(outputs, dict) else outputs
            choices = [
                choice for batch in batches
                for choice in sorted(batch["choices"], key=lambda item: item["index"])
            ]
            for choice, prompt in zip(choices, tokens, strict=True):
                probabilities = choice.get("logprobs")
                if probabilities is None or any(
                    len(probabilities[field]) != len(prompt) + 1
                    for field in ("token_logprobs", "top_logprobs")
                ):
                    raise ValueError(
                        "Likelihood scoring requires echoed logprobs for every prompt token "
                        "and one generated token; check the service's Completions support and tokenizer"
                    )
            return super().parse_logprobs(outputs, tokens=tokens, ctxlens=ctxlens, **kwargs)

    class ServiceEvaluation(ServiceAuthentication, LocalChatCompletion):
        """Dispatch actual harness request types without changing task scoring or aggregation."""

        @cached_property
        def completions(self):
            """Load the likelihood tokenizer only when a task needs text probabilities or a text template."""
            options = {**config.model_args, "base_url": f"{service['api_root']}/completions"}
            options.setdefault("tokenizer_backend", "huggingface")
            if options["tokenizer_backend"] == "huggingface" and not options.get("tokenizer"):
                source, tokenizer = service["tokenizer_identity"]
                if source != "hf":
                    from benchmarks.datasets.huggingface import resolve_tokenizer_path

                    tokenizer = resolve_tokenizer_path(tokenizer, source=source)
                options["tokenizer"] = tokenizer
            return LikelihoodCompletion.create_from_arg_obj(options, {"batch_size": config.batch_size})

        def likelihood_template(self, messages, add_generation_prompt=True):
            """Render the selected model template for text likelihoods, including mixed-request tasks."""
            return self.completions.tokenizer.apply_chat_template(
                messages, tokenize=False, add_generation_prompt=add_generation_prompt,
                continue_final_message=not add_generation_prompt,
                chat_template=self.completions.chat_template(config.apply_chat_template or True),
            )

        def loglikelihood(self, requests, **kwargs):
            """Delegate candidate scores; chat contexts from mixed-request tasks use the same model template."""
            requests = [
                replace(request, arguments=(
                    self.likelihood_template(json.loads(request.args[0].prompt)), request.args[1],
                )) if isinstance(request.args[0], JsonChatStr) else request
                for request in requests
            ]
            self.completions.set_cache_hook(self.cache_hook)
            return self.completions.loglikelihood(requests, **kwargs)

        def loglikelihood_rolling(self, requests, **kwargs):
            """Let the native tokenizer, rolling windows and likelihood aggregation score raw documents."""
            self.completions.set_cache_hook(self.cache_hook)
            return self.completions.loglikelihood_rolling(requests, **kwargs)

        def generate_until(self, requests, disable_tqdm: bool = False):
            """Reuse individual completed generations and let the native chat adapter produce missing draws."""
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

    # Chat payloads stay as messages; tokenizer choices belong to likelihood requests.
    chat_options = {**config.model_args, "tokenizer_backend": None, "tokenized_requests": False}
    chat_options.pop("tokenizer", None)
    model = ServiceEvaluation.create_from_arg_obj(chat_options, {"batch_size": config.batch_size})
    manager = config.process_tasks(config.metadata)
    native_load = manager.load

    def build_requests(task, build, **options):
        """Choose formatting per task before native request-cache lookup and instance construction."""
        chat_task = task.OUTPUT_TYPE == "generate_until"
        templated = chat_task or (
            task.OUTPUT_TYPE != "loglikelihood_rolling" and bool(config.apply_chat_template)
        )
        template = None
        tokenizer_name = ""
        if chat_task:
            template = model.apply_chat_template
            tokenizer_name = "foretoken-chat"
        elif templated:
            template = model.likelihood_template
            tokenizer_name = (
                f"{model.completions.tokenizer.name_or_path}:"
                f"{config.model_args.get('revision', 'main')}:{config.apply_chat_template}"
            )
        options.update(
            apply_chat_template=templated,
            fewshot_as_multiturn=templated and config.fewshot_as_multiturn is not False,
            chat_template=template,
            tokenizer_name=tokenizer_name,
        )
        task.set_config("metadata", {
            **(task.get_config("metadata") or {}),
            "foretoken": {
                "prompt_format": "chat_messages" if chat_task else (
                    "chat_template" if templated else "raw_text"
                ),
                **({"chat_template": config.apply_chat_template} if templated and not chat_task else {}),
            },
        })
        build(**options)
        if resume and any(instance.request_type != "generate_until" for instance in task.instances):
            raise ValueError("--resume supports lm-eval text generation tasks, not likelihood or perplexity tasks")

    def load_tasks(tasks):
        """Adapt only this invocation's task instances, preserving native groups and dataset loading."""
        loaded = native_load(tasks)
        for task in loaded["tasks"].values():
            task.build_all_requests = partial(build_requests, task, task.build_all_requests)
        return loaded

    manager.load = load_tasks
    progress = LmEvalResponses(Path(directory).parent) if config.use_cache is None else nullcontext(None)
    with progress as responses:
        _execute_lm_eval(config, model, manager)


def _execute_lm_eval(config, model, manager) -> None:
    """Call the public harness evaluator once and preserve native reporting and publication options."""
    from lm_eval import simple_evaluate
    from lm_eval.loggers import EvaluationTracker, TrackioLogger
    from lm_eval.utils import handle_non_serializable

    os.environ["TOKENIZERS_PARALLELISM"] = "false"
    config.hf_hub_log_args["output_path"] = config.output_path
    if os.environ.get("HF_TOKEN"):
        config.hf_hub_log_args["token"] = os.environ["HF_TOKEN"]
    tracker = EvaluationTracker(**config.hf_hub_log_args)
    trackio = TrackioLogger(config.trackio_args) if config.trackio_args else None
    try:
        results = simple_evaluate(
            model=model, model_args=config.model_args, tasks=config.tasks,
            num_fewshot=config.num_fewshot, batch_size=config.batch_size,
            max_batch_size=config.max_batch_size, device=config.device,
            use_cache=config.use_cache,
            cache_requests=config.cache_requests.get("cache_requests", False),
            rewrite_requests_cache=config.cache_requests.get("rewrite_requests_cache", False),
            delete_requests_cache=config.cache_requests.get("delete_requests_cache", False),
            limit=config.limit, samples=config.samples, check_integrity=config.check_integrity,
            write_out=config.write_out, log_samples=config.log_samples,
            evaluation_tracker=tracker, system_instruction=config.system_instruction,
            apply_chat_template=config.apply_chat_template,
            fewshot_as_multiturn=config.fewshot_as_multiturn,
            gen_kwargs=config.gen_kwargs, task_manager=manager, verbosity=config.verbosity,
            predict_only=config.predict_only,
            random_seed=config.seed[0] if config.seed else None,
            numpy_random_seed=config.seed[1] if config.seed else None,
            torch_random_seed=config.seed[2] if config.seed else None,
            fewshot_random_seed=config.seed[3] if config.seed else None,
            confirm_run_unsafe_code=config.confirm_run_unsafe_code, metadata=config.metadata,
        )
        if results is None:
            return
        samples = results.pop("samples") if config.log_samples else None
        if config.show_config:
            print(json.dumps(results, indent=2, default=handle_non_serializable, ensure_ascii=False))
        tracker.save_results_aggregated(results=results, samples=samples)
        if config.log_samples:
            for task_name in results["configs"]:
                tracker.save_results_samples(task_name=task_name, samples=samples[task_name])
        if tracker.push_results_to_hub or tracker.push_samples_to_hub:
            tracker.recreate_metadata_card()
        if trackio is not None:
            # Native CLI treats optional tracking failures as non-fatal; retain the saved scores.
            try:
                trackio.post_init(results)
                trackio.log_eval_result()
                if config.log_samples:
                    trackio.log_eval_samples(samples)
            except Exception as error:
                logging.getLogger(__name__).info("Logging to Trackio failed: %s", error)
    finally:
        if trackio is not None:
            trackio.finish()


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


def _restore_evaluation_progress(evaluator: str, model: str, source: str, native: Path) -> None:
    """Restore framework-specific progress without modifying the previous evaluation."""
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
        _restore_evaluation_progress(
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
