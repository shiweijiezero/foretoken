# SPDX-License-Identifier: Apache-2.0
# SPDX-FileCopyrightText: Copyright contributors to the Foretoken project


"""Typed benchmark configuration: one dataclass per concern."""

from __future__ import annotations

from dataclasses import asdict, dataclass, field
from typing import Any, Optional


@dataclass
class TargetConfig:
    """Inference service endpoint."""

    url: str
    model: str
    api_key: str = ""
    timeout: int = 300
    max_retries: int = 2


@dataclass
class LoadConfig:
    """Concurrency, request count, arrival rate, and open/closed loop."""

    parallel: list[int] = field(default_factory=lambda: [1])
    number: list[int] = field(default_factory=lambda: [100])
    # -1 = no pacing; >0 = Poisson pacing. Open-loop needs open_loop=True.
    rate: list[float] = field(default_factory=lambda: [-1.0])
    open_loop: bool = False

    @property
    def is_sweep(self) -> bool:
        return len(self.parallel) > 1 or len(self.number) > 1 or len(self.rate) > 1

    def sweep_points(self) -> list[tuple[int, int, float]]:
        """``(parallel, number, rate)`` triples for each load point."""
        if len(self.rate) > 1:
            if len(self.number) == len(self.rate):
                return [
                    (self.parallel[0], number, float(rate))
                    for number, rate in zip(self.number, self.rate)
                ]
            return [
                (self.parallel[0], self.number[0], float(rate))
                for rate in self.rate
            ]
        if len(self.parallel) > 1:
            if len(self.number) == len(self.parallel):
                return [
                    (parallel, number, float(self.rate[0]))
                    for parallel, number in zip(self.parallel, self.number)
                ]
            return [
                (parallel, self.number[0], float(self.rate[0]))
                for parallel in self.parallel
            ]
        if len(self.number) > 1:
            return [
                (self.parallel[0], number, float(self.rate[0]))
                for number in self.number
            ]
        return [(self.parallel[0], self.number[0], float(self.rate[0]))]

    def validate(self) -> None:
        """Reject incompatible open-loop / multi-list sweep combinations."""
        if self.open_loop and len(self.parallel) > 1:
            raise ValueError(
                "--open-loop uses unlimited concurrency; do not "
                "combine with a multi-value --parallel list. Use a "
                "single --parallel or sweep --number / --rate instead."
            )
        if len(self.rate) > 1 and len(self.parallel) > 1:
            raise ValueError(
                "Cannot sweep both --rate and --parallel at once; "
                "pass one multi-value list at a time."
            )
        if len(self.rate) > 1 and len(self.number) > 1:
            if len(self.number) != len(self.rate):
                raise ValueError(
                    "--number list must match --rate length when both "
                    f"are multi-value; got number={len(self.number)}, "
                    f"rate={len(self.rate)}."
                )
        elif len(self.parallel) > 1 and len(self.number) > 1:
            if len(self.number) != len(self.parallel):
                raise ValueError(
                    "--number list must match --parallel length when both "
                    f"are multi-value; got number={len(self.number)}, "
                    f"parallel={len(self.parallel)}."
                )


@dataclass
class GenerationConfig:
    """Sampling / generation parameters."""

    max_tokens: int = 128
    temperature: float = 0.0
    stream: bool = True


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
    - HuggingFace id: ``org/name:split`` (same row shape; split required,
      and may be a non-standard split/config name)

    Multiple JSONL/HF sources run sequentially; ``LoadConfig.number`` is the
    total request count across all of them.
    """

    dataset: list[str] = field(default_factory=list)
    dataset_offset: int = 0
    tokenizer_path: str = ""
    min_prompt_length: int = 0
    max_prompt_length: int = 131072
    prefix_length: int = 0
    apply_chat_template: Optional[bool] = None
    prompt: str = ""
    max_turns: Optional[int] = None

    @property
    def is_multi(self) -> bool:
        return len(self.dataset) > 1

    def resolve_apply_chat_template(self, url: str) -> bool:
        """Default to chat template when the URL is a chat/completions endpoint."""
        if self.apply_chat_template is not None:
            return self.apply_chat_template
        return url.rstrip("/").endswith("chat/completions")

@dataclass
class OutputConfig:
    """Result location and analysis knobs."""

    outputs_dir: str = "results"
    gpu_count: int = 1
    eval_suite: str = "none"
    sla_auto_tune: bool = False


@dataclass
class WandbConfig:
    """Weights & Biases logging."""

    enabled: bool = False
    project: str = "foretoken-bench"
    entity: str = ""
    run_name: str = ""


@dataclass
class EngineMetricsConfig:
    """Engine Prometheus ``/metrics`` collection."""

    collect: bool = True
    url: str = ""
    interval: float = 1.0


@dataclass
class ParamSweepConfig:
    """Serve × bench parameter product sweep."""

    serve_params: str = ""
    bench_params: str = ""
    link_vars: str = ""
    num_runs: int = 1
    dry_run: bool = False
    experiment_name: str = ""

    @property
    def enabled(self) -> bool:
        return bool(self.serve_params or self.bench_params)


@dataclass
class BenchConfig:
    """Root benchmark configuration (framework contract)."""

    target: TargetConfig
    load: LoadConfig = field(default_factory=LoadConfig)
    generation: GenerationConfig = field(default_factory=GenerationConfig)
    dataset: DatasetConfig = field(default_factory=DatasetConfig)
    output: OutputConfig = field(default_factory=OutputConfig)
    wandb: WandbConfig = field(default_factory=WandbConfig)
    engine: EngineMetricsConfig = field(default_factory=EngineMetricsConfig)
    param_sweep: ParamSweepConfig = field(default_factory=ParamSweepConfig)

    def validate(self) -> None:
        """Validate nested configs before a run starts."""
        self.load.validate()
        dataset = self.dataset
        if not dataset.prompt and not dataset.dataset:
            raise ValueError(
                "No workload source. Pass --prompt or --dataset "
                "(random | local JSONL path | HuggingFace id)."
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
        if dataset.prompt:
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
            dataset_label = (
                dataset.dataset[0] if dataset.dataset else "<none>"
            )
        open_loop = self.load.open_loop
        if open_loop:
            parallel_label = "unlimited (open-loop)"
        else:
            parallel_label = str(self.load.parallel)

        if len(self.load.rate) == 1:
            rate = float(self.load.rate[0])
            if rate > 0:
                mode = "open-loop" if open_loop else "closed-loop"
                rate_label = f"{rate:g} req/s ({mode}, Poisson pacing)"
            else:
                rate_label = "INF (no pacing)"
        else:
            rate_label = str(self.load.rate)

        return (
            "\n============================================\n"
            " Foretoken Benchmark\n"
            "============================================\n"
            f"Configuration:\n"
            f"  URL        : {self.target.url}\n"
            f"  Model      : {self.target.model}\n"
            f"  Parallel   : {parallel_label}\n"
            f"  Number     : {self.load.number}\n"
            f"  Rate       : {rate_label}\n"
            f"  Open Loop  : {open_loop}\n"
            f"  Stream     : {self.generation.stream}\n"
            f"  Dataset    : {dataset_label}\n"
        )

    def to_dict(self) -> dict[str, Any]:
        """Nested dict snapshot (e.g. param-sweep plan base config)."""
        return asdict(self)
