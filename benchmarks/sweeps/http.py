# SPDX-License-Identifier: Apache-2.0
# SPDX-FileCopyrightText: Copyright contributors to the Foretoken project

"""HTTP adapter for the shared parameter-sweep lifecycle."""

from __future__ import annotations

import logging
from contextlib import ExitStack
from dataclasses import replace
from pathlib import Path
from typing import Any, Callable

from benchmarks.config.benchmark import (
    BenchmarkConfig,
    ModelServiceSource,
    ParameterSweepConfig,
    normalize_output_token_limit,
    parse_duration_seconds,
)
from benchmarks.model_service import resolve_benchmark_service
from benchmarks.results.console import format_benchmark_config, log_sweep_results
from benchmarks.results.output import BenchmarkRun, wandb_run_timestamp
from benchmarks.runs.dispatch import run_benchmark_point
from benchmarks.runs.slo import SloAutoTuneBenchmark
from benchmarks.sweeps.core import (
    SweepAdapter,
    SweepDefinition,
    SweepPoint,
    _BENCHMARK_NAME,
    _PARAMETER_GROUP,
    load_sweep_points as load_core_sweep_points,
    run_sweep,
)

logger = logging.getLogger(__name__)


def _dataset_selectors(value: Any) -> list[str]:
    if isinstance(value, list):
        return [str(item) for item in value]
    return [str(value)]


def _preserve_value(value: Any) -> Any:
    return value


def _slo_criteria(value: Any) -> list[dict[str, str]]:
    """Keep one sweep choice as a group of criteria for the existing SLO config."""
    return [value] if isinstance(value, dict) else value


# Only HTTP request, generation, and workload choices are exposed by this adapter.
_SWEEP_FIELDS: dict[str, tuple[str, str, Callable[[Any], Any]]] = {
    "max_concurrency": ("load", "max_concurrency", int),
    "num_prompts": ("load", "request_count", int),
    "warmup_requests": ("load", "warmup_requests", int),
    "request_rate": ("load", "arrival_rate", float),
    "arrival_pattern": ("load", "arrival_pattern", str),
    "burstiness": ("load", "burstiness", float),
    "duration": ("load", "duration_seconds", parse_duration_seconds),
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
    "trace": ("trace", "trace_selector", str),
    "trace_start": ("trace", "start_offset_seconds", parse_duration_seconds),
    "trace_duration": ("trace", "duration_seconds", parse_duration_seconds),
    "trace_synthetic_prefix_reuse": ("trace", "synthetic_prefix_reuse", _preserve_value),
    "dataset_weights": ("workload", "dataset_weights", lambda value: [float(item) for item in (value.split(",") if isinstance(value, str) else value)]),
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
    "slo_params": ("slo", "params", _slo_criteria),
}


def _service_choice(value: Any) -> dict[str, str]:
    """Normalize a Kustomize path or named endpoint choice for one sweep method."""
    if isinstance(value, str):
        path = value.strip()
        if not path:
            raise ValueError("sweep service paths cannot be empty")
        return {"name": Path(path).name, "path": path}
    if not isinstance(value, dict):
        raise ValueError("sweep service must be a path or an object with name and path or url")
    unknown = set(value) - {"name", "path", "url", "model", "health_url"}
    if unknown:
        raise ValueError("Unsupported sweep service fields: " + ", ".join(sorted(unknown)))
    if not all(isinstance(item, str) for item in value.values()):
        raise ValueError("sweep service fields must be strings")
    if not value.get("name", "").strip() or bool(value.get("path")) == bool(value.get("url")):
        raise ValueError("sweep service requires a non-empty name and exactly one path or url")
    return value


class _HttpSweepAdapter(SweepAdapter[BenchmarkConfig]):
    """Apply and execute HTTP points while the core owns sweep orchestration."""

    axis_fields = {"service": _service_choice, **{key: field[2] for key, field in _SWEEP_FIELDS.items()}}

    def __init__(self, resources: ExitStack) -> None:
        self.resources = resources
        self.service = None
        self.selection = None

    def validate_record(self, record: SweepPoint, line_no: int) -> None:
        unknown = set(record) - set(self.axis_fields) - {_BENCHMARK_NAME, _PARAMETER_GROUP}
        if unknown:
            raise ValueError(f"Unsupported sweep keys on line {line_no}: " + ", ".join(sorted(unknown)))

    def apply_point(self, config: BenchmarkConfig, point: SweepPoint) -> BenchmarkConfig:
        section_updates: dict[str, dict[str, Any]] = {}
        for raw_key, raw_value in point.items():
            if raw_key in {_BENCHMARK_NAME, _PARAMETER_GROUP, "service"}:
                continue
            section, attribute, _ = _SWEEP_FIELDS[raw_key]
            section_updates.setdefault(section, {})[attribute] = raw_value
        updated = config
        for section, updates in section_updates.items():
            updated = replace(
                updated,
                **{section: replace(getattr(updated, section), **updates)},
            )
        if "service" in point:
            choice = _service_choice(point["service"])
            path = choice.get("path", "")
            if path:
                path = str((Path.cwd() / Path(path).expanduser()).resolve())
            updated = replace(updated, service=replace(
                config.service, name=choice["name"], kustomize_path=path,
                url=choice.get("url", ""), model=choice.get("model", config.service.model),
                health_url=choice.get("health_url", ""),
            ))
        return updated

    def validate_point(self, config: BenchmarkConfig) -> None:
        config.validate()

    def plan_base(self, config: BenchmarkConfig) -> dict[str, Any]:
        return config.to_dict()

    def group_name(self, config: BenchmarkConfig) -> str:
        return config.wandb.group.strip() or f"{config.sweep.experiment_name or 'sweep'}_{wandb_run_timestamp()}"

    def execute_point(
        self,
        config: BenchmarkConfig,
        *,
        output_dir: str,
        label: str,
        wandb_group: str,
        dry_run: bool,
    ) -> dict[str, Any]:
        point_config = replace(
            config, sweep=ParameterSweepConfig(),
            wandb=replace(config.wandb, group=wandb_group),
        )
        allow_multiple = bool(config.trace.trace_selector or config.is_multi_turn)
        selection = (config.service, allow_multiple)
        if selection != self.selection:
            self.resources.close()
            self.service = self.resources.enter_context(resolve_benchmark_service(point_config))
            self.selection = selection
        logger.info("%s", format_benchmark_config(point_config, self.service))
        if point_config.slo.search:
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
        metrics["gpu_count"] = (
            self.service.gpu_count
            if metrics["throughput"].get("generation_tokens_per_second_per_gpu") is not None
            else None
        )
        metrics["label"] = label
        return metrics


class ParameterSweepBenchmark:
    """Own sequential service selection while the shared sweep executes and publishes points."""

    def __init__(self, benchmark: BenchmarkConfig) -> None:
        self.benchmark = benchmark

    def run(self) -> BenchmarkRun:
        """Run HTTP repetitions and return the aggregate completion result."""
        sweep = self.benchmark.sweep
        with ExitStack() as resources:
            adapter = _HttpSweepAdapter(resources)
            definition = SweepDefinition(sweep.path, sweep.num_runs, sweep.experiment_name)
            if sweep.path:
                combinations = load_core_sweep_points(definition, adapter)
            else:
                definition = SweepDefinition("", 1, sweep.experiment_name or "service-comparison")
                combinations = [
                    {
                        _BENCHMARK_NAME: f"service-{choice['name']}",
                        _PARAMETER_GROUP: "service-comparison",
                        "service": choice,
                    }
                    for choice in self.benchmark.service_choices
                ]
            # Complete all workloads for a method before releasing its temporary service.
            methods: dict[str, ModelServiceSource] = {}
            for point in combinations:
                source = adapter.apply_point(self.benchmark, point).service
                if source.name in methods and methods[source.name] != source:
                    raise ValueError(f"Sweep service name {source.name!r} refers to different services")
                methods[source.name] = source
            order = {name: index for index, name in enumerate(methods)}
            combinations.sort(key=lambda point: order[adapter.apply_point(self.benchmark, point).service.name])
            execution = run_sweep(
                self.benchmark, definition, adapter,
                mode="parameter_sweep", combinations=combinations,
            )
        if len(execution.points) > 1 and not self.benchmark.outputs.includes("quiet"):
            log_sweep_results(execution.points)
        return execution.run
