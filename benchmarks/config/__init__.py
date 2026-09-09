# SPDX-License-Identifier: Apache-2.0
# SPDX-FileCopyrightText: Copyright contributors to the Foretoken project

"""Define and validate the current HTTP benchmark configuration."""

from __future__ import annotations

import random
from dataclasses import asdict, dataclass, field, replace
from typing import Any, Optional

OutputTokenLimit = int | list[int]
DEFAULT_DEPLOYMENT_PROMPT = "Hello"
DEFAULT_WAIT_TIMEOUT = "15m"


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
class ChatCompletionsEndpoint:
    """Store endpoint choices supplied by the benchmark user."""

    url: str = ""
    model: str = ""
    api_key: str = "EMPTY"
    timeout_seconds: int = 300


@dataclass
class BenchmarkDeploymentConfig:
    """Store the optional Kustomize source and readiness timeout for one benchmark."""

    kustomize_path: str = ""
    wait_timeout: str = DEFAULT_WAIT_TIMEOUT


@dataclass
class HttpLoadSchedule:
    """Store the standard HTTP workload, concurrency limit, and arrival rate."""

    max_concurrency: int = 1
    request_count: int = 100
    # -1 sends as fast as possible; positive values use a Poisson arrival rate.
    arrival_rate: float = -1.0
    unbounded_concurrency: bool = False

    @staticmethod
    def validate_coordinates(*, max_concurrency: int, arrival_rate: float) -> None:
        """Reject workload coordinates that would block or cannot express the requested schedule."""
        if max_concurrency < 1:
            raise ValueError(
                f"--parallel must be >= 1; got {max_concurrency}"
            )
        rate_value = float(arrival_rate)
        if rate_value != -1 and rate_value <= 0:
            raise ValueError(
                "--rate must be -1 (send as fast as possible) or > 0; "
                f"got {arrival_rate}"
            )

    def validate(self) -> None:
        """Validate a configured standard HTTP workload."""
        self.validate_coordinates(
            max_concurrency=int(self.max_concurrency),
            arrival_rate=float(self.arrival_rate),
        )
        if self.unbounded_concurrency and float(self.arrival_rate) == -1:
            raise ValueError(
                "--open-loop requires a positive --rate; EvalScope does not "
                "define an unbounded as-fast-as-possible schedule"
            )
        if self.request_count < 1:
            raise ValueError(
                f"--number must be >= 1, got {self.request_count}"
            )


@dataclass
class ChatCompletionsGeneration:
    """Store Chat Completions generation parameters applied to each measured request."""

    max_tokens: OutputTokenLimit = 128
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
    fixed_prompt: str = ""
    # -1 means the complete conversation; positive values truncate turns.
    max_turns: Optional[int] = -1

    @property
    def has_multiple_datasets(self) -> bool:
        """Return whether the workload declares multiple request datasets."""
        return len(self.dataset_selectors) > 1


@dataclass
class ArrivalTraceSchedule:
    """Store the trace selection, time window, and in-flight request limit for timestamp replay."""

    trace_selector: str = ""
    start_offset_seconds: float = 0.0
    duration_seconds: Optional[float] = None
    max_concurrency: Optional[int] = None
    synthetic_prefix_reuse: bool = False


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
    run_name: str = ""


@dataclass
class ParameterSweepConfig:
    """Store JSONL parameter sweep settings for a Foretoken deployment experiment."""

    bench_params: str = ""
    num_runs: int = 1
    experiment_name: str = ""


@dataclass
class HttpBenchmarkConfig:
    """Store the service, workload, and output configuration for the current HTTP benchmark."""

    deployment: BenchmarkDeploymentConfig = field(
        default_factory=BenchmarkDeploymentConfig
    )
    endpoint: ChatCompletionsEndpoint = field(
        default_factory=ChatCompletionsEndpoint
    )
    load_schedule: HttpLoadSchedule = field(default_factory=HttpLoadSchedule)
    generation: ChatCompletionsGeneration = field(
        default_factory=ChatCompletionsGeneration
    )
    request_dataset: ChatRequestDataset = field(
        default_factory=ChatRequestDataset
    )
    arrival_trace: ArrivalTraceSchedule = field(
        default_factory=ArrivalTraceSchedule
    )
    outputs: BenchmarkOutputConfig = field(default_factory=BenchmarkOutputConfig)
    wandb: WandbRunConfig = field(default_factory=WandbRunConfig)
    parameter_sweep: ParameterSweepConfig = field(
        default_factory=ParameterSweepConfig
    )

    @property
    def resolved_dataset(self) -> ChatRequestDataset:
        """Resolve prompt precedence and deployment defaults without changing user input."""
        dataset = self.request_dataset
        if self.arrival_trace.trace_selector:
            return dataset
        if dataset.fixed_prompt:
            return replace(dataset, dataset_selectors=[])
        if self.deployment.kustomize_path and not dataset.dataset_selectors:
            return replace(dataset, fixed_prompt=DEFAULT_DEPLOYMENT_PROMPT)
        return dataset

    @property
    def is_multi_turn(self) -> bool:
        """Return whether a dataset row owns a conversation lifecycle."""
        return not self.arrival_trace.trace_selector

    def validate(self) -> None:
        """Validate the selected HTTP workload before acquiring resources."""
        has_deployment = bool(self.deployment.kustomize_path)
        has_url = bool(self.endpoint.url)
        if has_deployment == has_url:
            raise ValueError("provide either PATH or --url")
        if has_url and not self.endpoint.model:
            raise ValueError("--model is required with --url")
        if self.parameter_sweep.bench_params and not has_deployment:
            raise ValueError("--bench-params requires a Foretoken Kustomize deployment")

        self.load_schedule.validate()
        self.outputs.validate()
        if "stream" in self.generation.extra_body:
            raise ValueError(
                "stream must be set via --stream/--no-stream, not extra_body"
            )

        dataset = self.resolved_dataset
        trace = self.arrival_trace
        has_trace = bool(trace.trace_selector)
        if (
            dataset.max_turns is not None
            and dataset.max_turns != -1
            and dataset.max_turns < 1
        ):
            raise ValueError(
                "--max-turns must be -1 (complete conversation) or >= 1"
            )
        if not has_trace:
            unsupported_body_fields = {
                "messages",
                "tools",
                "tool_choice",
                "parallel_tool_calls",
            } & self.generation.extra_body.keys()
            if unsupported_body_fields:
                names = ", ".join(sorted(unsupported_body_fields))
                raise ValueError(
                    "Multi-turn mode cannot use these --extra-body fields because "
                    f"they replace conversation or require a tool loop: {names}"
                )
        if not has_trace and not dataset.fixed_prompt and not dataset.dataset_selectors:
            raise ValueError(
                "No workload source. Pass --prompt or --dataset "
                "(random | local JSONL | org/name:split | "
                "hf://datasets/...)."
            )
        if self.parameter_sweep.bench_params and dataset.has_multiple_datasets:
            raise ValueError(
                "--bench-params cannot be combined with multiple --dataset sources"
            )
        if trace.synthetic_prefix_reuse and not has_trace:
            raise ValueError("--trace-synthetic-prefix-reuse requires --trace")
        if has_trace:
            if dataset.max_turns not in (None, -1):
                raise ValueError(
                    "--max-turns cannot be combined with --trace; trace replay "
                    "uses independent recorded requests"
                )
            if len(dataset.dataset_selectors) != 1:
                raise ValueError("--trace requires exactly one --dataset source")
            same_dataset = dataset.dataset_selectors[0] == trace.trace_selector

            if self.parameter_sweep.bench_params:
                raise ValueError("--trace cannot be combined with --bench-params")
            if dataset.fixed_prompt:
                raise ValueError(
                    "--trace requires --dataset; fixed --prompt payloads are "
                    "not supported"
                )
            if trace.start_offset_seconds < 0:
                raise ValueError("--trace-start must be >= 0")
            if trace.duration_seconds is not None and trace.duration_seconds <= 0:
                raise ValueError("--trace-duration must be > 0")
            if trace.max_concurrency is not None and trace.max_concurrency <= 0:
                raise ValueError("--trace-max-concurrency must be > 0")
            if (
                self.load_schedule.unbounded_concurrency
                or self.load_schedule.arrival_rate != -1
            ):
                raise ValueError(
                    "--trace uses record timestamps; omit --rate and --open-loop"
                )
            if (
                self.load_schedule.max_concurrency != 1
                or self.load_schedule.request_count != 100
            ):
                raise ValueError(
                    "--trace replays the selected trace window; use "
                    "--trace-max-concurrency instead of --parallel/--number"
                )
            if (
                same_dataset
                and dataset.row_offset
            ):
                raise ValueError(
                    "--dataset-offset is not supported when --trace and "
                    "--dataset use the same source"
                )
            if trace.synthetic_prefix_reuse:
                if dataset.dataset_selectors != ["random"]:
                    raise ValueError(
                        "--trace-synthetic-prefix-reuse requires --dataset random"
                    )
                if dataset.shared_prefix_tokens:
                    raise ValueError(
                        "--trace-synthetic-prefix-reuse cannot use --prefix-length"
                    )
        if dataset.fixed_prompt and dataset.has_multiple_datasets:
            raise ValueError(
                "--prompt cannot be combined with multiple --dataset values"
            )
        if dataset.has_multiple_datasets and "random" in dataset.dataset_selectors:
            raise ValueError(
                "--dataset random cannot be combined with other dataset sources"
            )
        if dataset.dataset_selectors == ["random"] and not dataset.tokenizer:
            raise ValueError(
                "--tokenizer-path is required when --dataset random"
            )
        if dataset.row_offset < 0:
            raise ValueError("--dataset-offset must be >= 0")
        if not 0 <= dataset.random_seed <= 0xFFFFFFFF:
            raise ValueError("--random-seed must be between 0 and 4294967295")
        if dataset.shared_prefix_tokens < 0:
            raise ValueError("--prefix-length must be >= 0")
        if dataset.minimum_prompt_tokens < 0:
            raise ValueError("--min-prompt-length must be >= 0")
        if dataset.maximum_prompt_tokens < dataset.minimum_prompt_tokens:
            raise ValueError(
                "--max-prompt-length must be >= --min-prompt-length"
            )

    def to_dict(self) -> dict[str, Any]:
        """Serialize only user configuration for result and W&B snapshots."""
        endpoint = asdict(self.endpoint)
        endpoint.pop("api_key", None)
        endpoint["timeout"] = endpoint.pop("timeout_seconds")

        load = {
            "parallel": self.load_schedule.max_concurrency,
            "number": self.load_schedule.request_count,
            "rate": self.load_schedule.arrival_rate,
            "open_loop": self.load_schedule.unbounded_concurrency,
        }
        dataset = {
            "dataset": list(self.resolved_dataset.dataset_selectors),
            "dataset_offset": self.resolved_dataset.row_offset,
            "tokenizer_path": self.resolved_dataset.tokenizer,
            "random_seed": self.resolved_dataset.random_seed,
            "min_prompt_length": self.resolved_dataset.minimum_prompt_tokens,
            "max_prompt_length": self.resolved_dataset.maximum_prompt_tokens,
            "prefix_length": self.resolved_dataset.shared_prefix_tokens,
            "prompt": self.resolved_dataset.fixed_prompt,
            "trace_path": self.arrival_trace.trace_selector,
            "trace_start": self.arrival_trace.start_offset_seconds,
            "trace_duration": self.arrival_trace.duration_seconds,
            "trace_max_concurrency": self.arrival_trace.max_concurrency,
            "trace_synthetic_prefix_reuse": (
                self.arrival_trace.synthetic_prefix_reuse
            ),
        }
        if self.is_multi_turn:
            dataset["multi_turn"] = True
            dataset["max_turns"] = self.resolved_dataset.max_turns
        output = asdict(self.outputs)
        return {
            "deployment": asdict(self.deployment),
            "endpoint": endpoint,
            "load": load,
            "generation": asdict(self.generation),
            "dataset": dataset,
            "output": output,
            "wandb": asdict(self.wandb),
            "param_sweep": asdict(self.parameter_sweep),
        }
