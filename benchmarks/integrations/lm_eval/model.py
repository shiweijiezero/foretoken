# SPDX-License-Identifier: Apache-2.0
# SPDX-FileCopyrightText: Copyright contributors to the Foretoken project

"""Adapt native harness requests and task formatting to a served model."""

from __future__ import annotations

from dataclasses import replace
from functools import cached_property, partial
import json

from lm_eval.models.api_models import JsonChatStr, LMEVAL_MODEL_NONE_ANSWER_PLACEHOLDER
from lm_eval.models.openai_completions import LocalChatCompletion, LocalCompletionsAPI


class ServiceAuthentication:
    """Share service authentication without putting credentials in harness configuration."""

    def __init__(self, *, service, **options):
        self.service = service
        super().__init__(**options)

    @property
    def api_key(self) -> str:
        return self.service["api_key"]

    @property
    def header(self) -> dict[str, str]:
        return {
            **super().header,
            **self.service["headers"],
            "Authorization": f"Bearer {self.api_key}",
        }


class LikelihoodCompletion(ServiceAuthentication, LocalCompletionsAPI):
    """Score text through the native adapter, requiring complete echoed token probabilities."""

    def __init__(self, *, responses=None, **options):
        self.responses = responses
        self.tokenizer_identity = (
            options.get("tokenizer"), options.get("tokenizer_backend"), options.get("revision", "main"),
        )
        super().__init__(**options)

    def _loglikelihood_tokens(self, requests, **kwargs):
        """Resume candidate and rolling windows while leaving batching, retries and scoring upstream."""
        if self.responses is None:
            return super()._loglikelihood_tokens(requests, **kwargs)
        inputs, context_lengths, _ = self.batch_loglikelihood_requests([requests])
        keys = [
            json.dumps({
                "request": self._create_payload(self.create_message([prompt]), generate=False, seed=self._seed),
                "context_length": context_length,
                "max_length": self.max_length,
                "tokenizer": self.tokenizer_identity,
            }, sort_keys=True, ensure_ascii=False)
            for prompt, context_length in zip(inputs, context_lengths)
        ]
        missing = {
            key: (key, context, continuation)
            for key, (_, context, continuation) in zip(keys, requests)
            if self.responses.get_likelihood(key) is None
        }
        if missing:
            previous_hook = self.cache_hook
            self.set_cache_hook(self.responses)
            try:
                # Rolling windows normally have no cache key; bind one before native dispatch.
                super()._loglikelihood_tokens(list(missing.values()), **kwargs)
            finally:
                self.set_cache_hook(previous_hook)
        return [self.responses.get_likelihood(key) for key in keys]

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
    """Own request dispatch and task formatting for one harness run; the caller owns the response store."""

    def __init__(self, *, config, responses=None, resume=False, **options):
        self.config = config
        self.responses = responses
        self.resume = resume
        super().__init__(**options)

    @cached_property
    def completions(self):
        """Load the native likelihood adapter and tokenizer only when a task needs them."""
        options = {**self.config.model_args, "base_url": f"{self.service['api_root']}/completions"}
        options.setdefault("tokenizer_backend", "huggingface")
        if options["tokenizer_backend"] == "huggingface" and not options.get("tokenizer"):
            source, tokenizer = self.service["tokenizer_identity"]
            if source != "hf":
                from benchmarks.datasets.huggingface import resolve_tokenizer_path

                tokenizer = resolve_tokenizer_path(tokenizer, source=source)
            options["tokenizer"] = tokenizer
        return LikelihoodCompletion.create_from_arg_obj(
            options, {
                "batch_size": self.config.batch_size, "service": self.service,
                "responses": self.responses,
            },
        )

    def likelihood_template(self, messages, add_generation_prompt=True):
        """Render the selected model template for text likelihoods, including mixed-request tasks."""
        return self.completions.tokenizer.apply_chat_template(
            messages, tokenize=False, add_generation_prompt=add_generation_prompt,
            continue_final_message=not add_generation_prompt,
            chat_template=self.completions.chat_template(self.config.apply_chat_template or True),
        )

    def loglikelihood(self, requests, **kwargs):
        """Delegate candidate scores, rendering chat contexts from mixed-request tasks as model text."""
        requests = [
            replace(request, arguments=(
                self.likelihood_template(json.loads(request.args[0].prompt)), request.args[1],
            )) if isinstance(request.args[0], JsonChatStr) else request
            for request in requests
        ]
        self.completions.set_cache_hook(self.cache_hook)
        return self.completions.loglikelihood(requests, **kwargs)

    def loglikelihood_rolling(self, requests, **kwargs):
        """Score raw documents with native tokenization, rolling windows and aggregation."""
        self.completions.set_cache_hook(self.cache_hook)
        return self.completions.loglikelihood_rolling(requests, **kwargs)

    def generate_until(self, requests, disable_tqdm: bool = False):
        """Reuse completed draws and send only missing generations to the native chat adapter."""
        if self.responses is None:
            return super().generate_until(requests, disable_tqdm=disable_tqdm)
        if any(len(request.args) != 2 for request in requests):
            if self.resume:
                raise ValueError("lm-eval resume supports text generation tasks")
            return super().generate_until(requests, disable_tqdm=disable_tqdm)
        slots = [self.responses.reserve(request.args) for request in requests]
        missing = [
            request for request, slot in zip(requests, slots)
            if self.responses.get(slot) is None
        ]
        if missing:
            previous_hook = self.cache_hook
            self.set_cache_hook(self.responses)
            try:
                super().generate_until(missing, disable_tqdm=disable_tqdm)
            finally:
                self.set_cache_hook(previous_hook)
        return [self.responses.get(slot) for slot in slots]

    async def get_batched_requests(self, requests, cache_keys, **kwargs):
        """Bind callbacks before dispatch so concurrent retries keep the same sample slot."""
        if self.responses is not None and self.cache_hook is self.responses:
            cache_keys = [self.responses.claim(key) for key in cache_keys]
        return await super().get_batched_requests(requests, cache_keys, **kwargs)

    def parse_generations(self, outputs, **kwargs):
        """Normalize null answers before synchronous or asynchronous completion callbacks."""
        values = super().parse_generations(outputs, **kwargs)
        if self.responses is None:
            return values
        return [LMEVAL_MODEL_NONE_ANSWER_PLACEHOLDER if value is None else value for value in values]

    def load_tasks(self, load, tasks):
        """Adapt this run's loaded task instances without changing native groups or dataset loading."""
        loaded = load(tasks)
        for task in loaded["tasks"].values():
            task.build_all_requests = partial(self.build_requests, task, task.build_all_requests)
        return loaded

    def build_requests(self, task, build, **options):
        """Select per-task formatting before native request-cache lookup and instance construction."""
        chat_task = task.OUTPUT_TYPE == "generate_until"
        templated = chat_task or (
            task.OUTPUT_TYPE != "loglikelihood_rolling" and bool(self.config.apply_chat_template)
        )
        template = None
        tokenizer_name = ""
        if chat_task:
            template = self.apply_chat_template
            tokenizer_name = "foretoken-chat"
        elif templated:
            template = self.likelihood_template
            tokenizer_name = (
                f"{self.completions.tokenizer.name_or_path}:"
                f"{self.config.model_args.get('revision', 'main')}:{self.config.apply_chat_template}"
            )
        options.update(
            apply_chat_template=templated,
            fewshot_as_multiturn=templated and self.config.fewshot_as_multiturn is not False,
            chat_template=template,
            tokenizer_name=tokenizer_name,
        )
        task.set_config("metadata", {
            **(task.get_config("metadata") or {}),
            "foretoken": {
                "prompt_format": "chat_messages" if chat_task else (
                    "chat_template" if templated else "raw_text"
                ),
                **({"chat_template": self.config.apply_chat_template} if templated and not chat_task else {}),
            },
        })
        build(**options)
