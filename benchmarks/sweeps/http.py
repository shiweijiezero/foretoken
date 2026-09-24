# SPDX-License-Identifier: Apache-2.0
# SPDX-FileCopyrightText: Copyright contributors to the Foretoken project

"""HTTP adapter for the shared parameter-sweep lifecycle."""

from __future__ import annotations

import asyncio
import logging
from dataclasses import replace
from typing import Any, Callable

from benchmarks.config.benchmark import (
    BenchmarkConfig,
    ParameterSweepConfig,
    normalize_output_token_limit,
)
from benchmarks.model_service import ModelService
from benchmarks.results.console import log_sweep_results
from benchmarks.results.output import BenchmarkRun, wandb_group_name
from benchmarks.results.pareto import plot_sweep_pareto
from benchmarks.runs.dispatch import run_benchmark_point
from benchmarks.runs.slo import SloAutoTuneBenchmark
from benchmarks.sweeps.core import (
    SweepAdapter,
    SweepDefinition,
    SweepPoint,
    _BENCHMARK_NAME,
    _PARAMETER_GROUP,
    expand_sweep_point,
    load_sweep_points as load_core_sweep_points,
    run_sweep,
    sweep_directory_name,
    sweep_point_name,
)

logger = logging.getLogger(__name__)


def _dataset_selectors(value: Any) -> list[str]:
    if isinstance(value, list):
        return [str(item) for item in value]
    return [str(value)]


def _preserve_value(value: Any) -> Any:
    return value


# Only HTTP request, generation, and workload choices are exposed by this adapter.
_SWEEP_FIELDS: dict[str, tuple[str, str, Callable[[Any], Any]]] = {
    "max_concurrency": ("load", "max_concurrency", int),
    "num_prompts": ("load", "request_count", int),
    "warmup_requests": ("load", "warmup_requests", int),
    "request_rate": ("load", "arrival_rate", float),
    "arrival_pattern": ("load", "arrival_pattern", str),
    "burstiness": ("load", "burstiness", float),
    "duration": ("load", "duration_seconds", float),
    "max_tokens": ("generation", "max_tokens", normalize_output_token_limit),
    "min_output_length": ("generation", "min_output_length", int),
    "max_output_length": ("generation", "max_output_length", int),
    "stream": ("generation", "stream", _preserve_value),
    "top_p": ("generation", "top_p", _preserve_value),
    "top_k": ("generation", "top_k", _preserve_value),
    "min_p": ("generation", "min_p", _preserve_value),
    "temperature": ("generation", "temperature", _preserve_value),
    "frequency_penalty": ("generation", "frequency_penalty", _preserve_value),
    "presence_penalty": ("generation", "presence_penalty", _preserve_value),
    "repetition_penalty": ("generation", "repetition_penalty", _preserve_value),
    "extra_body": ("generation", "extra_body", dict),
    "dataset": ("workload", "dataset_selectors", _dataset_selectors),
    "dataset_offset": ("workload", "row_offset", int),
    "tokenizer_path": ("workload", "tokenizer", str),
    "random_seed": ("workload", "random_seed", int),
    "min_prompt_length": ("workload", "minimum_prompt_tokens", int),
    "max_prompt_length": ("workload", "maximum_prompt_tokens", int),
    "prefix_length": ("workload", "shared_prefix_tokens", int),
    "apply_chat_template": ("workload", "apply_chat_template", _preserve_value),
    "prompt": ("workload", "fixed_prompt", str),
    "max_turns": ("workload", "max_turns", int),
    "conversation_history": ("workload", "conversation_history", str),
}


class _HttpSweepAdapter(SweepAdapter[BenchmarkConfig]):
    """Apply and execute HTTP points while the core owns sweep orchestration."""

    axis_fields = {key: field[2] for key, field in _SWEEP_FIELDS.items()}

    def __init__(self, service: ModelService) -> None:
        self.service = service

    def validate_record(self, record: SweepPoint, line_no: int) -> None:
        return None

    def apply_point(self, config: BenchmarkConfig, point: SweepPoint) -> BenchmarkConfig:
        section_updates: dict[str, dict[str, Any]] = {}
        for raw_key, raw_value in point.items():
            if raw_key in {_BENCHMARK_NAME, _PARAMETER_GROUP}:
                continue
            field = _SWEEP_FIELDS.get(str(raw_key))
            if field is None:
                allowed = ", ".join(sorted(_SWEEP_FIELDS))
                raise ValueError(
                    f"Unsupported sweep key {raw_key!r}. Only fields that change "
                    f"request execution may be swept; allowed keys: {allowed}"
                )
            section, attribute, coerce = field
            section_updates.setdefault(section, {})[attribute] = coerce(raw_value)
        updated = config
        for section, updates in section_updates.items():
            updated = replace(
                updated,
                **{section: replace(getattr(updated, section), **updates)},
            )
        return updated

    def validate_point(self, config: BenchmarkConfig) -> None:
        config.validate()

    def plan_base(self, config: BenchmarkConfig) -> dict[str, Any]:
        return config.to_dict()

    def group_name(self, config: BenchmarkConfig) -> str:
        return wandb_group_name(config, self.service)

    async def execute_point(
        self,
        config: BenchmarkConfig,
        *,
        output_dir: str,
        label: str,
        wandb_group: str,
        dry_run: bool,
    ) -> dict[str, Any]:
        point_config = replace(config, sweep=ParameterSweepConfig())
        if point_config.slo.params:
            result = SloAutoTuneBenchmark(
                point_config,
                self.service,
                label=label,
                output_dir=output_dir,
            ).run()
        else:
            result = run_benchmark_point(
                point_config,
                self.service,
                label=label,
                output_dir=output_dir,
                wandb_group=wandb_group,
            )
        metrics = dict(result.metrics)
        metrics["gpu_count"] = self.service.gpu_count
        metrics["label"] = label
        return metrics


def expand_load_points(item: SweepPoint) -> list[SweepPoint]:
    """Expand HTTP sweep fields for callers that inspect combinations directly."""
    return expand_sweep_point(item, _HttpSweepAdapter.axis_fields)


def load_sweep_points(path: str) -> list[SweepPoint]:
    """Load and expand an HTTP sweep file without running it."""
    adapter = _HttpSweepAdapter.__new__(_HttpSweepAdapter)
    return load_core_sweep_points(SweepDefinition(path, 1, ""), adapter)


def apply_sweep_point(benchmark: BenchmarkConfig, sweep_point: SweepPoint) -> BenchmarkConfig:
    """Apply one HTTP-owned point for compatibility with existing callers."""
    return _HttpSweepAdapter.__new__(_HttpSweepAdapter).apply_point(benchmark, sweep_point)


class ParameterSweepBenchmark:
    """Compose the shared sweep lifecycle with HTTP execution and Pareto output."""

    def __init__(self, benchmark: BenchmarkConfig, service: ModelService) -> None:
        self.benchmark = benchmark
        self.service = service

    def run(self) -> BenchmarkRun:
        """Run HTTP repetitions and return the aggregate completion result."""
        sweep = self.benchmark.sweep
        execution = asyncio.run(
            run_sweep(
                self.benchmark,
                SweepDefinition(sweep.path, sweep.num_runs, sweep.experiment_name),
                _HttpSweepAdapter(self.service),
                mode="parameter_sweep",
            )
        )
        if len(execution.points) > 1:
            if self.benchmark.outputs.includes("local"):
                fig_path = plot_sweep_pareto(execution.points, execution.experiment_dir)
                if fig_path is not None:
                    execution.artifacts["pareto"] = fig_path
                    logger.info("Pareto plot: %s", fig_path)
            if not self.benchmark.outputs.includes("quiet"):
                log_sweep_results(execution.points)
        totals = {
            name: sum(int(point.get(name, 0)) for point in execution.points)
            for name in ("request_num", "success_num", "failed_num")
        }
        return BenchmarkRun(
            record=execution.plan,
            metrics=totals,
            measurements=None,
            artifacts=execution.artifacts,
        )


__all__ = [
    "ParameterSweepBenchmark",
    "apply_sweep_point",
    "expand_load_points",
    "load_sweep_points",
    "sweep_directory_name",
    "sweep_point_name",
]
