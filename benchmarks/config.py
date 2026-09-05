# SPDX-License-Identifier: Apache-2.0
# SPDX-FileCopyrightText: Copyright contributors to the Foretoken project


"""Typed benchmark configuration: one dataclass per concern."""

from __future__ import annotations

from dataclasses import asdict, dataclass, field
from typing import Any, Optional

from evalscope.perf.multi_turn_args import IntOrRange


@dataclass
class EndpointConfig:
    """Inference service URL, model, and request options."""

    url: str
    model: str
    api_key: str = "EMPTY"
    timeout: int = 300
    max_retries: int = 2
    headers: dict[str, str] = field(default_factory=dict)


@dataclass
class LoadConfig:
    """Concurrency, request count, arrival rate, and open/closed loop."""

    parallel: int = 1
    number: int = 100
    # -1 = as fast as possible; >0 = Poisson arrivals.
    rate: float = -1.0
    open_loop: bool = False

    @staticmethod
    def validate_point(*, parallel: int, rate: float) -> None:
        """Reject load points that would hang or silently drop pacing."""
        if parallel < 1:
            raise ValueError(f"--parallel must be >= 1; got {parallel}")
        rate_value = float(rate)
        if rate_value != -1 and rate_value <= 0:
            raise ValueError(
                f"--rate must be -1 (send as fast as possible) or > 0; got {rate}"
            )

    def validate(self) -> None:
        """Validate load settings."""
        self.validate_point(parallel=int(self.parallel), rate=float(self.rate))
        if self.number < 1:
            raise ValueError(f"--number must be >= 1, got {self.number}")


@dataclass
class GenerationConfig:
    """Sampling and generation parameters for each request."""

    max_tokens: IntOrRange = 128
    stream: bool = True
    top_p: Optional[float] = None
    top_k: Optional[int] = None
    min_p: Optional[float] = None
    temperature: Optional[float] = None
    frequency_penalty: Optional[float] = None
    presence_penalty: Optional[float] = None
    repetition_penalty: Optional[float] = None
    extra_body: dict[str, Any] = field(default_factory=dict)

    def request_overrides(self) -> dict[str, Any]:
        """Return vLLM-compatible request fields with ``extra_body`` applied last."""
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


def allocate_dataset_counts(total: int, n: int) -> list[int]:
    """Split ``total`` requests across ``n`` datasets as evenly as possible."""
    if n <= 0:
        raise ValueError("dataset count must be > 0")
    if total < 0:
        raise ValueError("total request count must be >= 0")
    base, rem = divmod(total, n)
    return [base + (1 if i < rem else 0) for i in range(n)]


@dataclass
class DatasetConfig:
    """Workload source and prompt shaping.

    ``dataset`` is a list of unified source selectors:
    - ``random``: synthetic prompts (requires ``tokenizer_path``; alone only)
    - local JSONL path: one messages/prompt object per line
    - Hugging Face id: ``org/name:split`` (split required)
    - Hugging Face file URI: ``hf://datasets/{repo}[@{revision}]/{path}``
      (cached via Hub, then read as JSONL)

    Multiple JSONL/HF sources run sequentially; ``LoadConfig.number`` is the
    total request count across all of them.

    ``trace_path`` supplies a timestamped replay schedule. Trace payloads come
    from exactly one ``dataset`` source.
    """

    dataset: list[str] = field(default_factory=list)
    dataset_offset: int = 0
    tokenizer_path: str = ""
    random_seed: int = 0
    min_prompt_length: int = 0
    max_prompt_length: int = 131072
    prefix_length: int = 0
    apply_chat_template: Optional[bool] = None
    prompt: str = ""
    max_turns: Optional[int] = None
    trace_path: str = ""
    trace_start: float = 0.0
    trace_duration: Optional[float] = None
    trace_max_concurrency: Optional[int] = None
    trace_synthetic_prefix_reuse: bool = False

    @property
    def is_multi(self) -> bool:
        """Return whether the workload declares more than one payload source."""
        return len(self.dataset) > 1

    def resolve_apply_chat_template(self, url: str) -> bool:
        """Default to chat template when the URL is a chat/completions endpoint."""
        if self.apply_chat_template is not None:
            return self.apply_chat_template
        return url.rstrip("/").endswith("chat/completions")

@dataclass
class OutputConfig:
    """Result destinations, location, and analysis knobs."""

    destinations: tuple[str, ...] = ("local", "wandb")
    output_dir: str = "results"
    gpu_count: int = 1
    sla_auto_tune: bool = False

    def includes(self, destination: str) -> bool:
        return destination in self.destinations

    def validate(self) -> None:
        if not self.destinations:
            raise ValueError("--output must select at least one output option")
        allowed = {"local", "wandb", "quiet"}
        unknown = set(self.destinations) - allowed
        if unknown:
            names = ", ".join(sorted(unknown))
            raise ValueError(f"unknown --output option: {names}")
        if self.gpu_count < 1:
            raise ValueError(f"gpu_count must be >= 1, got {self.gpu_count}")


@dataclass
class WandbConfig:
    """Weights & Biases connection settings."""

    project: str = "foretoken-bench"
    entity: str = ""
    run_name: str = ""


@dataclass
class ParamSweepConfig:
    """Bench-params JSONL sweep for a Foretoken Kustomize deployment."""

    bench_params: str = ""
    num_runs: int = 1
    experiment_name: str = ""


@dataclass
class BenchConfig:
    """Root benchmark configuration (framework contract)."""

    endpoint: EndpointConfig
    load: LoadConfig = field(default_factory=LoadConfig)
    generation: GenerationConfig = field(default_factory=GenerationConfig)
    dataset: DatasetConfig = field(default_factory=DatasetConfig)
    output: OutputConfig = field(default_factory=OutputConfig)
    wandb: WandbConfig = field(default_factory=WandbConfig)
    param_sweep: ParamSweepConfig = field(default_factory=ParamSweepConfig)

    def validate(self) -> None:
        """Validate nested configs before a run starts."""
        self.load.validate()
        self.output.validate()
        dataset = self.dataset
        has_trace = bool(dataset.trace_path)
        if not has_trace and not dataset.prompt and not dataset.dataset:
            raise ValueError(
                "No workload source. Pass --prompt or --dataset "
                "(random | local JSONL | org/name:split | "
                "hf://datasets/...)."
            )
        if self.param_sweep.bench_params and dataset.is_multi:
            raise ValueError(
                "--bench-params cannot be combined with multiple --dataset sources"
            )
        if dataset.trace_synthetic_prefix_reuse and not has_trace:
            raise ValueError("--trace-synthetic-prefix-reuse requires --trace")
        if has_trace:
            from benchmarks.workload.hf_dataset import same_dataset_source

            if self.param_sweep.bench_params:
                raise ValueError("--trace cannot be combined with --bench-params")
            if dataset.prompt:
                raise ValueError(
                    "--trace requires --dataset; fixed --prompt payloads are "
                    "not supported"
                )
            if len(dataset.dataset) != 1:
                raise ValueError("--trace requires exactly one --dataset source")
            if dataset.trace_start < 0:
                raise ValueError("--trace-start must be >= 0")
            if dataset.trace_duration is not None and dataset.trace_duration <= 0:
                raise ValueError("--trace-duration must be > 0")
            if (
                dataset.trace_max_concurrency is not None
                and dataset.trace_max_concurrency <= 0
            ):
                raise ValueError("--trace-max-concurrency must be > 0")
            if self.load.open_loop or self.load.rate != -1:
                raise ValueError(
                    "--trace uses record timestamps; omit --rate and --open-loop"
                )
            if self.load.parallel != 1 or self.load.number != 100:
                raise ValueError(
                    "--trace replays the selected trace window; use "
                    "--trace-max-concurrency instead of --parallel/--number"
                )
            if (
                same_dataset_source(dataset.dataset[0], dataset.trace_path)
                and dataset.dataset_offset
            ):
                raise ValueError(
                    "--dataset-offset is not supported when --trace and "
                    "--dataset use the same source"
                )
            if dataset.trace_synthetic_prefix_reuse:
                if dataset.dataset != ["random"]:
                    raise ValueError(
                        "--trace-synthetic-prefix-reuse requires --dataset random"
                    )
                if dataset.prefix_length:
                    raise ValueError(
                        "--trace-synthetic-prefix-reuse cannot use --prefix-length"
                    )
        if dataset.prompt and dataset.is_multi:
            raise ValueError(
                "--prompt cannot be combined with multiple --dataset values"
            )
        if dataset.is_multi and "random" in dataset.dataset:
            raise ValueError(
                "--dataset random cannot be combined with other dataset sources"
            )
        if dataset.dataset == ["random"] and not dataset.tokenizer_path:
            raise ValueError(
                "--tokenizer-path is required when --dataset random"
            )
        if not 0 <= dataset.random_seed <= 0xFFFFFFFF:
            raise ValueError("--random-seed must be between 0 and 4294967295")
        if (
            dataset.dataset == ["random"]
            and dataset.max_prompt_length < dataset.min_prompt_length
        ):
            raise ValueError(
                "--max-prompt-length must be >= --min-prompt-length"
            )

    def summary(self) -> str:
        """Human-readable config banner for the console."""
        dataset = self.dataset
        if dataset.trace_path:
            dataset_label = (
                f"trace={dataset.trace_path}, payload={dataset.dataset[0]}"
            )
        elif dataset.prompt:
            dataset_label = "prompt=<fixed>"
        elif dataset.dataset == ["random"]:
            dataset_label = (
                f"random "
                f"(prefix={dataset.prefix_length}, "
                f"min={dataset.min_prompt_length}, "
                f"max={dataset.max_prompt_length})"
            )
        elif dataset.is_multi:
            dataset_label = f"{dataset.dataset} (total number across all)"
        else:
            dataset_label = dataset.dataset[0] if dataset.dataset else "<none>"

        if dataset.trace_path:
            parallel_line = ""
            number_label = "trace-driven"
            rate_label = "trace timestamps"
            open_loop_line = ""
            duration = (
                "until end"
                if dataset.trace_duration is None
                else f"{dataset.trace_duration:g}s"
            )
            trace_lines = (
                f"  Trace Window: start={dataset.trace_start:g}s, "
                f"duration={duration}\n"
                "  Trace concurrency: "
                f"{dataset.trace_max_concurrency or 'no limit'}\n"
            )
            if dataset.trace_synthetic_prefix_reuse:
                trace_lines += "  Trace Prefix: synthetic hash-id blocks\n"
        else:
            open_loop = self.load.open_loop
            parallel_label = (
                "no concurrency limit"
                if open_loop
                else str(self.load.parallel)
            )
            parallel_line = f"  Concurrency: {parallel_label}\n"
            number_label = str(self.load.number)
            rate = float(self.load.rate)
            if rate > 0:
                mode = "open-loop" if open_loop else "closed-loop"
                rate_label = f"{rate:g} req/s ({mode}, Poisson arrivals)"
            else:
                rate_label = "no rate limit"
            open_loop_line = f"  Open-loop  : {open_loop}\n"
            trace_lines = ""

        return (
            "\n===== Foretoken Benchmark Configuration ====\n"
            f"  URL        : {self.endpoint.url}\n"
            f"  Model      : {self.endpoint.model}\n"
            f"{parallel_line}"
            f"  Requests   : {number_label}\n"
            f"  Arrival rate: {rate_label}\n"
            f"{open_loop_line}"
            f"  Stream     : {self.generation.stream}\n"
            f"  Dataset    : {dataset_label}\n"
            f"{trace_lines}"
            "============================================\n"
        )

    def to_dict(self) -> dict[str, Any]:
        """Return the serializable benchmark config without credentials."""
        config = asdict(self)
        config["endpoint"].pop("api_key", None)
        return config
