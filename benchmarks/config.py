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

    @property
    def primary_parallel(self) -> int:
        return self.parallel[0]

    @property
    def primary_number(self) -> int:
        return self.number[0]

    @property
    def primary_rate(self) -> float:
        return float(self.rate[0])

    def number_for(self, index: int) -> int:
        if len(self.number) == 1:
            return self.number[0]
        return self.number[index] if index < len(self.number) else self.number[-1]

    def rate_for(self, index: int) -> float:
        if len(self.rate) == 1:
            return float(self.rate[0])
        return float(self.rate[index] if index < len(self.rate) else self.rate[-1])

    def sweep_points(self) -> list[tuple[int, int, float]]:
        """``(parallel, number, rate)`` triples; rate list wins over parallel."""
        base_parallel = self.primary_parallel
        base_rate = self.primary_rate
        if len(self.rate) > 1:
            return [
                (base_parallel, self.number_for(i), self.rate_for(i))
                for i in range(len(self.rate))
            ]
        if len(self.parallel) > 1:
            return [
                (parallel, self.number_for(i), base_rate)
                for i, parallel in enumerate(self.parallel)
            ]
        if len(self.number) > 1:
            return [(base_parallel, number, base_rate) for number in self.number]
        return [(base_parallel, self.primary_number, base_rate)]

    def validate(self) -> None:
        """Reject incompatible open-loop / multi-list sweep combinations."""
        if self.open_loop and len(self.parallel) > 1:
            raise ValueError(
                "--open-loop uses unlimited concurrency (EvalScope "
                "parallel=-1); do not combine with a multi-value "
                "--parallel list. Use a single --parallel or sweep "
                "--number / --rate instead."
            )
        if len(self.rate) > 1 and len(self.parallel) > 1:
            raise ValueError(
                "Cannot sweep both --rate and --parallel lists at once; "
                "sweep_points prioritizes --rate and would silently ignore "
                "all but the first --parallel value. Pass one multi-value "
                "list at a time."
            )


@dataclass
class GenerationConfig:
    """Sampling / generation parameters."""

    max_tokens: int = 128
    temperature: float = 0.0
    stream: bool = True


@dataclass
class DatasetConfig:
    """Dataset plugin, paths, and prompt shaping."""

    dataset: str = "openqa"
    dataset_path: list[str] = field(default_factory=list)
    dataset_offset: int = 0
    tokenizer_path: str = ""
    min_prompt_length: int = 0
    max_prompt_length: int = 131072
    prefix_length: int = 0
    apply_chat_template: Optional[bool] = None
    prompt: str = ""
    max_turns: Optional[int] = None

    @property
    def primary_dataset_path(self) -> str:
        return self.dataset_path[0] if self.dataset_path else ""


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

    def summary(self) -> str:
        """Human-readable config banner for the console."""
        path = (
            f" path={self.dataset.primary_dataset_path}"
            if self.dataset.dataset_path
            else ""
        )
        open_loop = self.load.open_loop
        if open_loop:
            parallel_s = "unlimited (open-loop)"
        else:
            parallel_s = str(self.load.parallel)

        if len(self.load.rate) == 1:
            rate = float(self.load.rate[0])
            if rate > 0:
                mode = "open-loop" if open_loop else "closed-loop"
                rate_s = f"{rate:g} req/s ({mode}, Poisson pacing)"
            else:
                rate_s = "INF (no pacing)"
        else:
            rate_s = str(self.load.rate)

        return (
            "\n==============================\n"
            " Foretoken Benchmark\n"
            "==============================\n"
            f"Configuration:\n"
            f"  URL        : {self.target.url}\n"
            f"  Model      : {self.target.model}\n"
            f"  Parallel   : {parallel_s}\n"
            f"  Number     : {self.load.number}\n"
            f"  Rate       : {rate_s}\n"
            f"  Open Loop  : {open_loop}\n"
            f"  Stream     : {self.generation.stream}\n"
            f"  Dataset    : mode={self.dataset.dataset}{path}\n"
        )

    def to_dict(self) -> dict[str, Any]:
        """Nested dict snapshot (e.g. param-sweep plan base config)."""
        return asdict(self)
