# SPDX-License-Identifier: Apache-2.0
# SPDX-FileCopyrightText: Copyright contributors to the Foretoken project

"""定义并校验当前 HTTP 性能评测配置。"""

from __future__ import annotations

import random
from dataclasses import asdict, dataclass, field
from typing import Any, Optional

OutputTokenLimit = int | list[int]


def normalize_output_token_limit(value: int | list[int]) -> OutputTokenLimit:
    """把 CLI 中的固定 max-tokens 或闭区间规范化为内部表示。"""
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
    """保存 OpenAI-compatible Chat Completions 地址和传输选项。"""

    url: str
    model: str
    api_key: str = "EMPTY"
    timeout_seconds: int = 300
    headers: dict[str, str] = field(default_factory=dict)


@dataclass
class HttpLoadSchedule:
    """保存标准 HTTP 负载的工作量、并发上限和到达率。"""

    max_concurrency: int = 1
    request_count: int = 100
    # -1 表示尽快发送；正数表示泊松到达率。
    arrival_rate: float = -1.0
    unbounded_concurrency: bool = False

    @staticmethod
    def validate_coordinates(*, max_concurrency: int, arrival_rate: float) -> None:
        """拒绝会阻塞或无法表达预期节奏的负载坐标。"""
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
        """校验一个已配置的标准 HTTP 负载。"""
        self.validate_coordinates(
            max_concurrency=int(self.max_concurrency),
            arrival_rate=float(self.arrival_rate),
        )
        if self.request_count < 1:
            raise ValueError(
                f"--number must be >= 1, got {self.request_count}"
            )


@dataclass
class ChatCompletionsGeneration:
    """保存应用到每个测量请求的 Chat Completions 生成参数。"""

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
        """返回固定上限，或从配置的闭区间中采样。"""
        if isinstance(self.max_tokens, list):
            return random.randint(self.max_tokens[0], self.max_tokens[1])
        return self.max_tokens

    def request_overrides(self) -> dict[str, Any]:
        """返回生成请求字段，并让 ``extra_body`` 最后覆盖。"""
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
    """保存 Chat Completions 请求或交互式对话的数据集选择。"""

    dataset_selectors: list[str] = field(default_factory=list)
    row_offset: int = 0
    tokenizer: str = ""
    random_seed: int = 0
    minimum_prompt_tokens: int = 0
    maximum_prompt_tokens: int = 131072
    shared_prefix_tokens: int = 0
    fixed_prompt: str = ""
    max_turns: Optional[int] = None

    @property
    def has_multiple_datasets(self) -> bool:
        """返回该负载是否声明了多个请求数据集。"""
        return len(self.dataset_selectors) > 1

    @property
    def is_multi_turn(self) -> bool:
        """返回是否执行交互式对话；指定轮数上限即明确该语义。"""
        return self.max_turns is not None


@dataclass
class ArrivalTraceSchedule:
    """保存记录时间回放的轨迹选择、时间窗口和在途请求上限。"""

    trace_selector: str = ""
    start_offset_seconds: float = 0.0
    duration_seconds: Optional[float] = None
    max_concurrency: Optional[int] = None
    synthetic_prefix_reuse: bool = False


@dataclass
class BenchmarkOutputConfig:
    """保存 HTTP 性能结果目标和本地目录。"""

    destinations: tuple[str, ...] = ("local", "wandb")
    output_dir: str = "results"

    def includes(self, destination: str) -> bool:
        """返回指定输出目标是否启用。"""
        return destination in self.destinations

    def validate(self) -> None:
        """校验已选择的 HTTP 性能结果目标。"""
        if not self.destinations:
            raise ValueError("--output must select at least one output option")
        allowed = {"local", "wandb", "quiet"}
        unknown = set(self.destinations) - allowed
        if unknown:
            names = ", ".join(sorted(unknown))
            raise ValueError(f"unknown --output option: {names}")


@dataclass
class WandbRunConfig:
    """保存性能测量使用的 Weights & Biases run 设置。"""

    project: str = "foretoken-bench"
    entity: str = ""
    run_name: str = ""


@dataclass
class ParameterSweepConfig:
    """保存一个 Foretoken 部署实验的 JSONL 参数扫描设置。"""

    bench_params: str = ""
    num_runs: int = 1
    experiment_name: str = ""


@dataclass
class HttpBenchmarkConfig:
    """保存当前 HTTP 性能评测的服务、负载和结果配置。"""

    endpoint: ChatCompletionsEndpoint
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
    serving_gpu_count: int = 1

    def validate(self) -> None:
        """在获取资源前校验所选 HTTP 性能负载。"""
        self.load_schedule.validate()
        self.outputs.validate()
        if "stream" in self.generation.extra_body:
            raise ValueError(
                "stream must be set via --stream/--no-stream, not extra_body"
            )
        if self.serving_gpu_count < 1:
            raise ValueError(
                f"gpu_count must be >= 1, got {self.serving_gpu_count}"
            )

        dataset = self.request_dataset
        trace = self.arrival_trace
        has_trace = bool(trace.trace_selector)
        if dataset.max_turns is not None and dataset.max_turns != -1 and dataset.max_turns < 1:
            raise ValueError("--max-turns must be -1 (complete conversation) or >= 1")
        if dataset.is_multi_turn:
            if has_trace:
                raise ValueError(
                    "Multi-turn mode cannot be combined with --trace; trace replay "
                    "treats recorded rows as independent requests"
                )
            if dataset.fixed_prompt:
                raise ValueError(
                    "Multi-turn mode requires a dataset conversation; --prompt is "
                    "a single independent request"
                )
            if self.load_schedule.unbounded_concurrency:
                raise ValueError(
                    "Multi-turn mode cannot be combined with --open-loop because "
                    "each turn waits for the previous model response"
                )
            if self.load_schedule.arrival_rate != -1:
                raise ValueError(
                    "Multi-turn mode currently requires --rate -1; EvalScope's "
                    "multi-turn rate is a per-worker inter-turn delay, not the "
                    "global request arrival rate exposed by Foretoken"
                )
            if dataset.dataset_selectors == ["random"]:
                raise ValueError(
                    "--dataset random is not supported with conversation mode; use "
                    "a local or Hugging Face conversation dataset"
                )
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
            if len(dataset.dataset_selectors) != 1:
                raise ValueError("--trace requires exactly one --dataset source")
            same_dataset = dataset.dataset_selectors[0] == trace.trace_selector
            if (
                not same_dataset
                and "/" in dataset.dataset_selectors[0]
                and "/" in trace.trace_selector
                and not dataset.dataset_selectors[0].startswith("/")
                and not trace.trace_selector.startswith("/")
            ):
                from benchmarks.performance.huggingface_datasets import (
                    same_dataset_selector,
                )

                same_dataset = same_dataset_selector(
                    dataset.dataset_selectors[0], trace.trace_selector
                )

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
        """返回不含凭据且兼容现有结果文件的配置结构。"""
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
            "dataset": list(self.request_dataset.dataset_selectors),
            "dataset_offset": self.request_dataset.row_offset,
            "tokenizer_path": self.request_dataset.tokenizer,
            "random_seed": self.request_dataset.random_seed,
            "min_prompt_length": self.request_dataset.minimum_prompt_tokens,
            "max_prompt_length": self.request_dataset.maximum_prompt_tokens,
            "prefix_length": self.request_dataset.shared_prefix_tokens,
            "prompt": self.request_dataset.fixed_prompt,
            "trace_path": self.arrival_trace.trace_selector,
            "trace_start": self.arrival_trace.start_offset_seconds,
            "trace_duration": self.arrival_trace.duration_seconds,
            "trace_max_concurrency": self.arrival_trace.max_concurrency,
            "trace_synthetic_prefix_reuse": (
                self.arrival_trace.synthetic_prefix_reuse
            ),
        }
        if self.request_dataset.is_multi_turn:
            dataset["multi_turn"] = True
            dataset["max_turns"] = self.request_dataset.max_turns
        output = {
            **asdict(self.outputs),
            "gpu_count": self.serving_gpu_count,
        }
        return {
            "endpoint": endpoint,
            "load": load,
            "generation": asdict(self.generation),
            "dataset": dataset,
            "output": output,
            "wandb": asdict(self.wandb),
            "param_sweep": asdict(self.parameter_sweep),
        }
