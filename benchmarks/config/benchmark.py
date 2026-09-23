# SPDX-License-Identifier: Apache-2.0
# SPDX-FileCopyrightText: Copyright contributors to the Foretoken project

"""Define and validate the benchmark configuration."""

from __future__ import annotations

import math
import random
from dataclasses import dataclass, field, replace
from typing import Any, Optional

OutputTokenLimit = int | list[int]


def normalize_output_token_limit(value: int | list[int]) -> OutputTokenLimit:
    """Normalize a fixed CLI max-tokens value or range into the internal representation."""
    if isinstance(value, int):
        if value < 0:
            raise ValueError(f"max-tokens must be >= 0, got {value}")
        return value
    if len(value) == 1:
        return normalize_output_token_limit(value[0])
    if len(value) != 2:
        raise ValueError(
            f"--max-tokens accepts 1 or 2 values [min max], got {value}"
        )
    minimum, maximum = value
    if minimum > maximum:
        raise ValueError(f"max-tokens range min must be <= max, got {value}")
    if minimum < 0:
        raise ValueError(f"max-tokens range values must be >= 0, got {value}")
    return value


@dataclass
class ModelServiceSource:
    """Store the model service chosen by the user: a Kustomize deployment or an existing URL."""

    kustomize_path: str = ""
    url: str = ""
    health_url: str = ""
    model: str = ""
    api_key: str = "EMPTY"
    timeout_seconds: int = 300
    max_retries: int = 0
    wait_timeout: str = "15m"

    def validate(self) -> None:
        """Require exactly one service source and an explicit model for a URL."""
        if bool(self.kustomize_path) == bool(self.url):
            raise ValueError("provide either PATH or --url")
        if self.max_retries < 0:
            raise ValueError("--max-retries must be >= 0")
        if self.url and not self.model:
            raise ValueError("--model is required with --url")


@dataclass
class HttpLoadSchedule:
    """Store the HTTP request budget, concurrency, and arrival process."""

    max_concurrency: int = 1
    request_count: int | None = 100
    arrival_rate: float = -1.0
    arrival_pattern: str = "poisson"
    burstiness: float = 1.0
    warmup_requests: int = 0
    duration_seconds: float | None = None

    def validate(self) -> None:
        """Reject load coordinates that would block or cannot express the requested schedule."""
        if self.max_concurrency != -1 and self.max_concurrency < 1:
            raise ValueError(
                f"--max-concurrency must be -1 or >= 1; got {self.max_concurrency}"
            )
        rate_value = float(self.arrival_rate)
        if not math.isfinite(rate_value) or (rate_value != -1 and rate_value <= 0):
            raise ValueError(
                "--request-rate must be -1 (send as fast as possible) or > 0; "
                f"got {self.arrival_rate}"
            )
        if self.request_count is not None and self.request_count < 1:
            raise ValueError(
                f"--num-prompts must be >= 1, got {self.request_count}"
            )
        if self.arrival_pattern not in {"constant", "poisson", "gamma"}:
            raise ValueError(
                "--arrival-pattern must be constant, poisson, or gamma"
            )
        if not math.isfinite(self.burstiness) or self.burstiness <= 0:
            raise ValueError("--burstiness must be finite and > 0")
        if self.arrival_pattern == "gamma" and rate_value == -1:
            raise ValueError("gamma arrival requires --request-rate > 0")
        if self.arrival_pattern == "constant" and rate_value == -1:
            raise ValueError("constant arrival requires --request-rate > 0")
        if self.warmup_requests < 0:
            raise ValueError("--warmup-requests must be >= 0")
        if self.duration_seconds is not None and (
            not math.isfinite(self.duration_seconds) or self.duration_seconds <= 0
        ):
            raise ValueError("--duration must be > 0 seconds")
        if (
            self.duration_seconds is not None
            and self.arrival_rate == -1
            and self.max_concurrency == -1
        ):
            raise ValueError(
                "--duration with an unrated workload requires --max-concurrency > 0"
            )


@dataclass
class ChatCompletionsGeneration:
    """Store Chat Completions generation parameters applied to each measured request."""

    max_tokens: OutputTokenLimit = 4096
    min_output_length: int | None = None
    max_output_length: int | None = None
    stream: bool = True
    top_p: Optional[float] = None
    top_k: Optional[int] = None
    min_p: Optional[float] = None
    temperature: Optional[float] = None
    frequency_penalty: Optional[float] = None
    presence_penalty: Optional[float] = None
    repetition_penalty: Optional[float] = None
    extra_body: dict[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        self.max_tokens = normalize_output_token_limit(self.max_tokens)

    def validate(self) -> None:
        """Validate output length bounds and generation fields owned by dedicated options."""
        lengths = (self.min_output_length, self.max_output_length)
        if any(value is not None for value in lengths):
            if any(value is None for value in lengths):
                raise ValueError("--min-output-length and --max-output-length must be supplied together")
            if not 1 <= self.min_output_length <= self.max_output_length:
                raise ValueError("output lengths must satisfy 1 <= min <= max")
            conflicts = {
                "max_tokens", "max_completion_tokens", "min_tokens",
                "ignore_eos", "stop", "stop_token_ids",
            } & self.extra_body.keys()
            if conflicts:
                raise ValueError(
                    "output length control conflicts with --extra-body fields: "
                    + ", ".join(sorted(conflicts))
                )
        if "stream" in self.extra_body:
            raise ValueError(
                "stream must be set via --stream/--no-stream, not extra_body"
            )

    def sample_output_length(self) -> int | None:
        """Choose an exact synthetic output target, or None for ordinary generation."""
        if self.min_output_length is None:
            return None
        return random.randint(self.min_output_length, self.max_output_length)

    def sample_max_tokens(self) -> int:
        """Return the fixed limit or sample from the configured inclusive range."""
        if isinstance(self.max_tokens, list):
            return random.randint(self.max_tokens[0], self.max_tokens[1])
        return self.max_tokens

    def request_overrides(self) -> dict[str, Any]:
        """Return generation request fields, with ``extra_body`` applied last."""
        sampling = {
            "top_p": self.top_p,
            "top_k": self.top_k,
            "min_p": self.min_p,
            "temperature": self.temperature,
            "frequency_penalty": self.frequency_penalty,
            "presence_penalty": self.presence_penalty,
            "repetition_penalty": self.repetition_penalty,
        }
        return {
            **{key: value for key, value in sampling.items() if value is not None},
            **self.extra_body,
        }


@dataclass
class ChatRequestDataset:
    """Store the dataset selection for Chat Completions requests or interactive conversations."""

    dataset_selectors: list[str] = field(default_factory=list)
    row_offset: int = 0
    tokenizer: str = ""
    random_seed: int = 0
    minimum_prompt_tokens: int = 0
    maximum_prompt_tokens: int = 131072
    shared_prefix_tokens: int = 0
    apply_chat_template: bool = False
    fixed_prompt: str = ""
    # -1 means the complete conversation; positive values truncate turns.
    max_turns: Optional[int] = -1

    @property
    def has_multiple_datasets(self) -> bool:
        """Return whether the workload declares multiple request datasets."""
        return len(self.dataset_selectors) > 1

    def validate(self) -> None:
        """Validate dataset selection, conversation truncation, and prompt-length settings."""
        if (
            self.max_turns is not None
            and self.max_turns != -1
            and self.max_turns < 1
        ):
            raise ValueError(
                "--max-turns must be -1 (complete conversation) or >= 1"
            )
        if self.fixed_prompt and self.has_multiple_datasets:
            raise ValueError(
                "--prompt cannot be combined with multiple --dataset values"
            )
        if self.has_multiple_datasets and "random" in self.dataset_selectors:
            raise ValueError(
                "--dataset random cannot be combined with other dataset sources"
            )
        if self.dataset_selectors == ["random"] and not self.tokenizer:
            raise ValueError(
                "--tokenizer-path is required when --dataset random"
            )
        if self.row_offset < 0:
            raise ValueError("--dataset-offset must be >= 0")
        if not 0 <= self.random_seed <= 0xFFFFFFFF:
            raise ValueError("--random-seed must be between 0 and 4294967295")
        if self.shared_prefix_tokens < 0:
            raise ValueError("--prefix-length must be >= 0")
        if self.minimum_prompt_tokens < 0:
            raise ValueError("--min-prompt-length must be >= 0")
        if self.maximum_prompt_tokens < self.minimum_prompt_tokens:
            raise ValueError(
                "--max-prompt-length must be >= --min-prompt-length"
            )


@dataclass
class ArrivalTraceSchedule:
    """Store the trace selection, time window, and in-flight request limit for timestamp replay."""

    trace_selector: str = ""
    start_offset_seconds: float = 0.0
    duration_seconds: Optional[float] = None
    max_concurrency: Optional[int] = None
    synthetic_prefix_reuse: bool = False

    def validate(self) -> None:
        """Validate the replay window and options that only apply with a selected trace."""
        if not self.trace_selector:
            if self.synthetic_prefix_reuse:
                raise ValueError("--trace-synthetic-prefix-reuse requires --trace")
            return
        if not math.isfinite(self.start_offset_seconds) or self.start_offset_seconds < 0:
            raise ValueError("--trace-start must be >= 0")
        if self.duration_seconds is not None and (not math.isfinite(self.duration_seconds) or self.duration_seconds <= 0):
            raise ValueError("--trace-duration must be > 0")
        if self.max_concurrency is not None and self.max_concurrency <= 0:
            raise ValueError("--trace-max-concurrency must be > 0")


@dataclass
class BenchmarkOutputConfig:
    """Store HTTP benchmark output destinations and the local directory."""

    destinations: tuple[str, ...] = ("local", "wandb")
    output_dir: str = "results"

    def includes(self, destination: str) -> bool:
        """Return whether the specified output destination is enabled."""
        return destination in self.destinations

    def validate(self) -> None:
        """Validate the selected HTTP benchmark output destinations."""
        if not self.destinations:
            raise ValueError("--output must select at least one output option")
        allowed = {"local", "wandb", "quiet"}
        unknown = set(self.destinations) - allowed
        if unknown:
            names = ", ".join(sorted(unknown))
            raise ValueError(f"unknown --output option: {names}")


@dataclass
class WandbRunConfig:
    """Store the Weights & Biases run settings used for benchmark measurements."""

    project: str = "foretoken-bench"
    entity: str = ""
    group: str = ""
    run_name: str = ""


@dataclass
class ParameterSweepConfig:
    """Store JSONL parameter sweep settings for a Foretoken deployment experiment."""

    path: str = ""
    num_runs: int = 1
    experiment_name: str = ""


@dataclass
class SloTuneConfig:
    """Store SLO search criteria and concurrency bounds."""

    params: list[dict[str, str]] | None = None
    num_runs: int = 1
    upper_bound: Optional[int] = None
    lower_bound: int = 1

    def validate(self) -> None:
        """Validate SLO criteria and search bounds before starting a workload."""
        if self.params is None:
            return
        if not self.params or any(
            not isinstance(group, dict) or not group for group in self.params
        ):
            raise ValueError(
                "--slo-params must be a non-empty JSON array of non-empty objects"
            )
        if any(
            not all(
                isinstance(metric, str) and isinstance(criterion, str)
                for metric, criterion in group.items()
            )
            for group in self.params
        ):
            raise ValueError("--slo-params metric names and criteria must be strings")
        if self.num_runs < 1:
            raise ValueError("--num-runs must be >= 1")
        if self.lower_bound < 1:
            raise ValueError("--slo-lower-bound must be >= 1")
        if (
            self.upper_bound is not None
            and self.upper_bound < self.lower_bound
        ):
            raise ValueError("--slo-upper-bound must be >= --slo-lower-bound")


@dataclass
class BenchmarkProfileConfig:
    """Select one runtime-owned capture accompanying a generated workload."""

    engine: str
    duration: str


@dataclass
class BenchmarkConfig:
    """Store the service, workload, load, and output configuration for one benchmark command."""

    service: ModelServiceSource = field(default_factory=ModelServiceSource)
    workload: ChatRequestDataset = field(default_factory=ChatRequestDataset)
    load: HttpLoadSchedule = field(default_factory=HttpLoadSchedule)
    generation: ChatCompletionsGeneration = field(
        default_factory=ChatCompletionsGeneration
    )
    trace: ArrivalTraceSchedule = field(default_factory=ArrivalTraceSchedule)
    outputs: BenchmarkOutputConfig = field(default_factory=BenchmarkOutputConfig)
    wandb: WandbRunConfig = field(default_factory=WandbRunConfig)
    sweep: ParameterSweepConfig = field(default_factory=ParameterSweepConfig)
    slo: SloTuneConfig = field(default_factory=SloTuneConfig)
    profile: BenchmarkProfileConfig | None = None

    @property
    def resolved_workload(self) -> ChatRequestDataset:
        """Resolve prompt precedence and deployment defaults without changing user input."""
        workload = self.workload
        if self.trace.trace_selector:
            return workload
        if workload.fixed_prompt:
            return replace(workload, dataset_selectors=[])
        if self.service.kustomize_path and not workload.dataset_selectors:
            return replace(workload, fixed_prompt="Hello")
        return workload

    @property
    def is_multi_turn(self) -> bool:
        """Return whether the resolved workload is conversation-driven."""
        workload = self.resolved_workload
        return (
            not self.trace.trace_selector
            and bool(workload.dataset_selectors)
            and workload.dataset_selectors != ["random"]
        )

    def slo_search_start(self) -> int:
        """Resolve the first concurrency probe for an SLO binary search."""
        if self.trace.trace_selector:
            configured = self.trace.max_concurrency
        else:
            configured = self.load.max_concurrency
            if configured == -1:
                raise ValueError(
                    "--slo-params requires --max-concurrency >= 1"
                )
        low = self.slo.lower_bound
        high = self.slo.upper_bound
        if configured is None or low >= configured:
            if high is not None:
                start = (low + high) // 2
            else:
                start = low
        else:
            start = configured
        if start < low or (high is not None and start > high):
            bounds = (
                f"[{low}, {high}]"
                if high is not None
                else f">= {low}"
            )
            raise ValueError(
                "SLO search start must be within "
                f"[--slo-lower-bound, --slo-upper-bound]; got {start} not in "
                f"{bounds}"
            )
        return start

    def validate(self) -> None:
        """Validate each section, then the rules that span sections, before acquiring resources."""
        self.service.validate()
        if self.profile is not None and not self.service.kustomize_path:
            raise ValueError("--profile requires a Foretoken Kustomize deployment")
        if self.sweep.path and not self.service.kustomize_path:
            raise ValueError("--sweep requires a Foretoken Kustomize deployment")
        self.load.validate()
        self.outputs.validate()
        self.generation.validate()
        workload = self.resolved_workload
        workload.validate()
        if self.generation.min_output_length is not None and workload.dataset_selectors != ["random"]:
            raise ValueError("output length control requires --dataset random")
        if (
            self.is_multi_turn
            and self.load.arrival_pattern in {"constant", "gamma"}
            and self.load.arrival_rate == -1
        ):
            raise ValueError(
                "multi-turn constant and gamma arrivals require --request-rate > 0"
            )
        self.trace.validate()
        if self.trace.trace_selector and self.load.arrival_pattern != "poisson":
            raise ValueError("--trace cannot be combined with generated arrival patterns")
        self.slo.validate()

        if self.slo.params:
            self.slo_search_start()

        trace = self.trace
        has_trace = bool(trace.trace_selector)
        if not has_trace and self.load.request_count is None and self.load.duration_seconds is None:
            raise ValueError("--num-prompts is required unless --duration is set")
        if not has_trace:
            unsupported_body_fields = {"messages"} & self.generation.extra_body.keys()
            if unsupported_body_fields:
                names = ", ".join(sorted(unsupported_body_fields))
                raise ValueError(
                    "Conversation mode cannot use these --extra-body fields because "
                    f"they replace the conversation: {names}"
                )
            if not workload.fixed_prompt and not workload.dataset_selectors:
                raise ValueError(
                    "No workload source. Pass --prompt or --dataset "
                    "(random | local JSONL | org/name[:split] | "
                    "hf://datasets/...)."
                )
        if has_trace:
            if workload.max_turns not in (None, -1):
                raise ValueError(
                    "--max-turns cannot be combined with --trace; trace replay "
                    "uses independent recorded requests"
                )
            if len(workload.dataset_selectors) != 1:
                raise ValueError("--trace requires exactly one --dataset source")
            same_dataset = workload.dataset_selectors[0] == trace.trace_selector

            if workload.fixed_prompt:
                raise ValueError(
                    "--trace requires --dataset; fixed --prompt payloads are "
                    "not supported"
                )
            if self.load.arrival_rate != -1:
                raise ValueError(
                    "--trace uses record timestamps; omit --request-rate"
                )
            if (
                not self.slo.params
                and (
                    self.load.max_concurrency != HttpLoadSchedule().max_concurrency
                    or (
                        self.load.request_count is not None
                        and self.load.request_count != HttpLoadSchedule().request_count
                    )
                    or self.load.arrival_rate != HttpLoadSchedule().arrival_rate
                    or self.load.duration_seconds is not None
                )
            ):
                raise ValueError(
                    "--trace replays the selected trace window; use "
                    "--trace-max-concurrency instead of --max-concurrency/--num-prompts"
                )
            if same_dataset and workload.row_offset:
                raise ValueError(
                    "--dataset-offset is not supported when --trace and "
                    "--dataset use the same source"
                )
            if trace.synthetic_prefix_reuse:
                if workload.dataset_selectors != ["random"]:
                    raise ValueError(
                        "--trace-synthetic-prefix-reuse requires --dataset random"
                    )
                if workload.shared_prefix_tokens:
                    raise ValueError(
                        "--trace-synthetic-prefix-reuse cannot use --prefix-length"
                    )

    def to_dict(self) -> dict[str, Any]:
        """Serialize only user configuration for result and W&B snapshots."""
        service = {
            "kustomize_path": self.service.kustomize_path,
            "url": self.service.url,
            "health_url": self.service.health_url,
            "model": self.service.model,
            "timeout": self.service.timeout_seconds,
            "max_retries": self.service.max_retries,
            "wait_timeout": self.service.wait_timeout,
        }

        load = {
            "max_concurrency": self.load.max_concurrency,
            "num_prompts": self.load.request_count,
            "request_rate": self.load.arrival_rate,
            "arrival_pattern": self.load.arrival_pattern,
            "burstiness": self.load.burstiness,
            "warmup_requests": self.load.warmup_requests,
            "duration": self.load.duration_seconds,
        }
        workload = self.resolved_workload
        dataset = {
            "dataset": list(workload.dataset_selectors),
            "dataset_offset": workload.row_offset,
            "tokenizer_path": workload.tokenizer,
            "random_seed": workload.random_seed,
            "min_prompt_length": workload.minimum_prompt_tokens,
            "max_prompt_length": workload.maximum_prompt_tokens,
            "prefix_length": workload.shared_prefix_tokens,
            "apply_chat_template": workload.apply_chat_template,
            "prompt": workload.fixed_prompt,
            "trace_path": self.trace.trace_selector,
            "trace_start": self.trace.start_offset_seconds,
            "trace_duration": self.trace.duration_seconds,
            "trace_max_concurrency": self.trace.max_concurrency,
            "trace_synthetic_prefix_reuse": self.trace.synthetic_prefix_reuse,
        }
        if not self.trace.trace_selector:
            dataset["max_turns"] = workload.max_turns
        return {
            "service": service,
            "load": load,
            "generation": {
                "max_tokens": self.generation.max_tokens,
                "min_output_length": self.generation.min_output_length,
                "max_output_length": self.generation.max_output_length,
                "stream": self.generation.stream,
                "top_p": self.generation.top_p,
                "top_k": self.generation.top_k,
                "min_p": self.generation.min_p,
                "temperature": self.generation.temperature,
                "frequency_penalty": self.generation.frequency_penalty,
                "presence_penalty": self.generation.presence_penalty,
                "repetition_penalty": self.generation.repetition_penalty,
                "extra_body": self.generation.extra_body,
            },
            "dataset": dataset,
            "output": {
                "destinations": self.outputs.destinations,
                "output_dir": self.outputs.output_dir,
            },
            "wandb": {
                "project": self.wandb.project,
                "entity": self.wandb.entity,
                "group": self.wandb.group,
                "run_name": self.wandb.run_name,
            },
            "sweep": {
                "path": self.sweep.path,
                "num_runs": self.sweep.num_runs,
                "experiment_name": self.sweep.experiment_name,
            },
            "slo": {
                "params": self.slo.params,
                "num_runs": self.slo.num_runs,
                "upper_bound": self.slo.upper_bound,
                "lower_bound": self.slo.lower_bound,
            },
            "profile": (
                {"engine": self.profile.engine, "duration": self.profile.duration}
                if self.profile is not None else None
            ),
        }
