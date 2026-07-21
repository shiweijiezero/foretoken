"""BenchArguments: typed run config + CLI/string parsing helpers."""

from __future__ import annotations

from dataclasses import MISSING, asdict, dataclass, field, fields
from typing import Any, Optional


def parse_int_list(value: str) -> list[int]:
    return [int(x.strip()) for x in str(value).split(",") if x.strip()]


def parse_float_list(value: str) -> list[float]:
    return [float(x.strip()) for x in str(value).split(",") if x.strip()]


def parse_str_list(value: str) -> list[str]:
    return [x.strip() for x in str(value).split(",") if x.strip()]


def metric_parallel(item: dict[str, Any], default: int = 1) -> int:
    """Canonical ``parallel`` from metrics/row dict (incl. legacy keys)."""
    for key in ("parallel", "concurrency", "max_concurrency"):
        if item.get(key) is not None:
            return int(item[key])
    return default


def _field_default(name: str) -> Any:
    f = next(x for x in fields(BenchArguments) if x.name == name)
    if f.default_factory is not MISSING:
        return f.default_factory()
    if f.default is not MISSING:
        return f.default
    raise KeyError(name)


def _fmt_cli(x: Any) -> str:
    if isinstance(x, float) and x.is_integer():
        return str(int(x))
    return str(x)


def cli_default(name: str) -> Any:
    """Typer Option default from ``BenchArguments`` (lists → comma string)."""
    val = _field_default(name)
    if isinstance(val, list):
        return ",".join(_fmt_cli(x) for x in val)
    return val


def _as_int_list(value: Any, fallback: str) -> list[int]:
    if value is None:
        value = fallback
    return parse_int_list(value) if isinstance(value, str) else value


def _as_float_list(value: Any, fallback: str) -> list[float]:
    if value is None:
        value = fallback
    if isinstance(value, str):
        return parse_float_list(value) if value.strip() else [-1.0]
    return value


def _as_str_list(value: Any) -> list[str]:
    if value is None or value == "":
        return []
    return parse_str_list(value) if isinstance(value, str) else value


@dataclass
class BenchArguments:
    """Unified arguments for `foretoken bench`."""

    url: str
    model: str
    api_key: str = ""
    timeout: int = 300

    parallel: list[int] = field(default_factory=lambda: [1])
    number: list[int] = field(default_factory=lambda: [100])
    # -1 = no pacing; >0 = Poisson pacing. Open-loop needs open_loop=True.
    rate: list[float] = field(default_factory=lambda: [-1.0])
    open_loop: bool = False
    max_tokens: int = 128
    temperature: float = 0.0
    stream: bool = True

    # dataset: EvalScope plugin, or sequential|mixed with multi dataset_path
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

    # Gates for unimplemented modes (raise in main until Phase 2 / 0.5).
    sla_auto_tune: bool = False
    eval_suite: str = "none"

    gpu_count: int = 1
    outputs_dir: str = "results"

    wandb: bool = False
    wandb_project: str = "foretoken-bench"
    wandb_entity: str = ""
    wandb_run_name: str = ""

    collect_engine_metrics: bool = True
    engine_metrics_url: str = ""
    engine_metrics_interval: float = 1.0

    serve_params: str = ""
    bench_params: str = ""
    link_vars: str = ""
    num_runs: int = 1
    dry_run: bool = False
    experiment_name: str = ""

    @classmethod
    def from_cli(cls, **kwargs: Any) -> "BenchArguments":
        """Build from CLI kwargs; parse list-valued string flags."""
        gpu_iv = kwargs.pop("gpu_metrics_interval", None)
        if gpu_iv is not None:
            kwargs["engine_metrics_interval"] = float(gpu_iv)
        elif "engine_metrics_interval" in kwargs:
            kwargs["engine_metrics_interval"] = float(
                kwargs["engine_metrics_interval"]
            )

        kwargs["parallel"] = _as_int_list(
            kwargs.pop("parallel", None), cli_default("parallel")
        )
        kwargs["number"] = _as_int_list(
            kwargs.pop("number", None), cli_default("number")
        )
        kwargs["rate"] = _as_float_list(
            kwargs.pop("rate", None), cli_default("rate")
        )
        kwargs["dataset_path"] = _as_str_list(kwargs.pop("dataset_path", None))

        known = {f.name for f in fields(cls)}
        return cls(**{k: v for k, v in kwargs.items() if k in known})

    @property
    def is_param_sweep(self) -> bool:
        return bool(self.serve_params or self.bench_params)

    @property
    def primary_dataset_path(self) -> str:
        return self.dataset_path[0] if self.dataset_path else ""

    @property
    def is_multi_dataset(self) -> bool:
        mode = (self.dataset or "").strip().lower()
        return mode in ("sequential", "mixed") or len(self.dataset_path) > 1

    @property
    def is_sweep(self) -> bool:
        return (
            len(self.parallel) > 1
            or len(self.number) > 1
            or len(self.rate) > 1
        )

    def validate(self) -> None:
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

    @property
    def primary_parallel(self) -> int:
        return self.parallel[0]

    @property
    def primary_number(self) -> int:
        return self.number[0]

    @property
    def primary_rate(self) -> float:
        return float(self.rate[0]) if self.rate else -1.0

    def number_for(self, index: int) -> int:
        if len(self.number) == 1:
            return self.number[0]
        return self.number[index] if index < len(self.number) else self.number[-1]

    def rate_for(self, index: int) -> float:
        if len(self.rate) == 1:
            return float(self.rate[0])
        return float(
            self.rate[index] if index < len(self.rate) else self.rate[-1]
        )

    def sweep_points(self) -> list[tuple[int, int, float]]:
        """``(parallel, number, rate)`` triples; rate list wins over parallel."""
        if len(self.rate) > 1:
            p = self.primary_parallel
            return [
                (p, self.number_for(i), self.rate_for(i))
                for i in range(len(self.rate))
            ]
        if len(self.parallel) > 1:
            r = self.primary_rate
            return [
                (p, self.number_for(i), r)
                for i, p in enumerate(self.parallel)
            ]
        if len(self.number) > 1:
            return [
                (self.primary_parallel, n, self.primary_rate)
                for n in self.number
            ]
        return [
            (self.primary_parallel, self.primary_number, self.primary_rate)
        ]

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    def run_config(self) -> dict[str, Any]:
        """Config snapshot saved alongside a single / sweep run."""
        keys = (
            "url", "model", "timeout", "parallel", "number", "rate",
            "open_loop", "max_tokens", "temperature", "stream", "dataset",
            "dataset_path", "dataset_offset", "tokenizer_path",
            "min_prompt_length", "max_prompt_length", "prefix_length",
            "apply_chat_template", "prompt", "max_turns",
            "gpu_count", "eval_suite", "outputs_dir",
            "collect_engine_metrics", "engine_metrics_url",
            "engine_metrics_interval", "wandb",
        )
        return {k: getattr(self, k) for k in keys}
