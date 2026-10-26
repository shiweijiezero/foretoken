# SPDX-License-Identifier: Apache-2.0
# SPDX-FileCopyrightText: Copyright contributors to the Foretoken project

"""HTTP adapter for the shared parameter-sweep lifecycle."""

from __future__ import annotations

from collections.abc import Callable
from contextlib import ExitStack
from dataclasses import replace
from typing import Any, ClassVar, cast

from loguru import logger
from pydantic import ConfigDict, TypeAdapter, ValidationError

from benchmarks.config.benchmark import (
    BenchmarkConfig,
    ModelServiceSource,
    ParameterSweepConfig,
    normalize_output_token_limit,
    parse_duration_seconds,
    select_service,
    service_choice,
)
from benchmarks.config.decoding import (
    BOOLEAN,
    INTEGER,
    NUMBER,
    OPTIONAL_INTEGER,
    OPTIONAL_NUMBER,
    STRING,
)
from benchmarks.model_service import ModelService, ModelServiceResolver
from benchmarks.results.reporting.http import format_benchmark_config, log_sweep_results
from benchmarks.results.types import BenchmarkRun
from benchmarks.results.wandb.recording import wandb_run_timestamp
from benchmarks.runs.http import HttpBenchmark
from benchmarks.runs.slo import SloAutoTuneBenchmark
from benchmarks.sweeps.core import (
    _BENCHMARK_NAME,
    _PARAMETER_GROUP,
    SweepAdapter,
    SweepDefinition,
    SweepPoint,
    run_sweep,
)
from benchmarks.sweeps.core import (
    load_sweep_points as load_core_sweep_points,
)

_DATASET_SELECTORS = TypeAdapter(str | list[str], config=ConfigDict(strict=True, hide_input_in_errors=True))
_DATASET_WEIGHTS = TypeAdapter(str | list[float], config=ConfigDict(strict=True, hide_input_in_errors=True))
_WEIGHT_VALUES = TypeAdapter(list[float], config=ConfigDict(hide_input_in_errors=True))
_SLO_CRITERIA = TypeAdapter(dict[str, str] | list[dict[str, str]], config=ConfigDict(strict=True, hide_input_in_errors=True))
_EXTRA_BODY = TypeAdapter(dict[str, Any], config=ConfigDict(strict=True, hide_input_in_errors=True))


def _dataset_selectors(value: Any) -> list[str]:
    """Decode one source or an explicit mixed-source choice without coercing row selectors."""
    selected = _DATASET_SELECTORS.validate_python(value)
    return selected if isinstance(selected, list) else [selected]


def _dataset_weights(value: Any) -> list[float]:
    """Decode relative dataset weights, retaining the CLI's comma-separated syntax."""
    weights = _DATASET_WEIGHTS.validate_python(value)
    return _WEIGHT_VALUES.validate_python(weights.split(",")) if isinstance(weights, str) else weights


def _slo_criteria(value: Any) -> list[dict[str, str]]:
    """Keep one sweep choice as a group of criteria for the existing SLO config."""
    criteria = _SLO_CRITERIA.validate_python(value)
    return [criteria] if isinstance(criteria, dict) else criteria


# Only HTTP request, generation, and workload choices are exposed by this adapter.
_SWEEP_FIELDS: dict[str, tuple[str, str, Callable[[Any], Any]]] = {
    "max_concurrency": ("load", "max_concurrency", INTEGER.validate_python),
    "num_prompts": ("load", "request_count", INTEGER.validate_python),
    "warmup_requests": ("load", "warmup_requests", INTEGER.validate_python),
    "request_rate": ("load", "arrival_rate", NUMBER.validate_python),
    "arrival_pattern": ("load", "arrival_pattern", STRING.validate_python),
    "burstiness": ("load", "burstiness", NUMBER.validate_python),
    "duration": ("load", "duration_seconds", parse_duration_seconds),
    "max_tokens": ("generation", "max_tokens", normalize_output_token_limit),
    "min_output_length": ("generation", "min_output_length", INTEGER.validate_python),
    "max_output_length": ("generation", "max_output_length", INTEGER.validate_python),
    "stream": ("generation", "stream", BOOLEAN.validate_python),
    "top_p": ("generation", "top_p", OPTIONAL_NUMBER.validate_python),
    "top_k": ("generation", "top_k", OPTIONAL_INTEGER.validate_python),
    "min_p": ("generation", "min_p", OPTIONAL_NUMBER.validate_python),
    "temperature": ("generation", "temperature", OPTIONAL_NUMBER.validate_python),
    "frequency_penalty": ("generation", "frequency_penalty", OPTIONAL_NUMBER.validate_python),
    "presence_penalty": ("generation", "presence_penalty", OPTIONAL_NUMBER.validate_python),
    "repetition_penalty": ("generation", "repetition_penalty", OPTIONAL_NUMBER.validate_python),
    "extra_body": ("generation", "extra_body", _EXTRA_BODY.validate_python),
    "dataset": ("workload", "dataset_selectors", _dataset_selectors),
    "trace": ("trace", "trace_selector", STRING.validate_python),
    "trace_start": ("trace", "start_offset_seconds", parse_duration_seconds),
    "trace_duration": ("trace", "duration_seconds", parse_duration_seconds),
    "trace_synthetic_prefix_reuse": ("trace", "synthetic_prefix_reuse", BOOLEAN.validate_python),
    "dataset_weights": ("workload", "dataset_weights", _dataset_weights),
    "dataset_offset": ("workload", "row_offset", INTEGER.validate_python),
    "tokenizer_path": ("workload", "tokenizer", STRING.validate_python),
    "random_seed": ("workload", "random_seed", INTEGER.validate_python),
    "min_prompt_length": ("workload", "minimum_prompt_tokens", INTEGER.validate_python),
    "max_prompt_length": ("workload", "maximum_prompt_tokens", INTEGER.validate_python),
    "prefix_length": ("workload", "shared_prefix_tokens", INTEGER.validate_python),
    "apply_chat_template": ("workload", "apply_chat_template", BOOLEAN.validate_python),
    "prompt": ("workload", "fixed_prompt", STRING.validate_python),
    "max_turns": ("workload", "max_turns", INTEGER.validate_python),
    "conversation_history": ("workload", "conversation_history", STRING.validate_python),
    "slo_params": ("slo", "params", _slo_criteria),
}


class ParameterSweepBenchmark:
    """Own sequential service selection while the shared sweep executes and publishes points."""

    def __init__(self, benchmark: BenchmarkConfig) -> None:
        self.benchmark = benchmark

    def run(self) -> BenchmarkRun:
        """Run HTTP repetitions and return the aggregate completion result."""
        resources = ExitStack()
        adapter = _HttpSweepAdapter(resources)
        definition, combinations = self._prepare_points(adapter)
        execution = run_sweep(
            self.benchmark, definition, adapter,
            mode="parameter_sweep", combinations=combinations,
            service_resources=resources,
        )
        if len(execution.points) > 1 and not self.benchmark.outputs.includes("quiet"):
            log_sweep_results(execution.points)
        return execution.run

    def _prepare_points(self, adapter: _HttpSweepAdapter) -> tuple[SweepDefinition, list[SweepPoint]]:
        """Combine workload points with CLI services unless a point selects its own service."""
        sweep = self.benchmark.sweep
        definition = SweepDefinition(sweep.path, sweep.num_runs, sweep.experiment_name, sweep.resume)
        choices = self.benchmark.service_choices
        if sweep.path:
            combinations = load_core_sweep_points(definition, adapter)
            if len(choices) > 1:
                services = [
                    {"health_url": self.benchmark.service.health_url, **choice}
                    for choice in choices
                ]
                combinations = [
                    {**point, "service": choice}
                    for point in combinations
                    for choice in ([point["service"]] if "service" in point else services)
                ]
        else:
            definition = SweepDefinition("", 1, sweep.experiment_name or "service-comparison", sweep.resume)
            combinations = [
                {
                    _BENCHMARK_NAME: f"service-{choice['name']}",
                    _PARAMETER_GROUP: "service-comparison",
                    "service": choice,
                }
                for choice in choices
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
        return definition, combinations


class _HttpSweepAdapter(SweepAdapter[BenchmarkConfig]):
    """Apply and execute HTTP points while the core owns sweep orchestration."""

    axis_fields: ClassVar[dict[str, Callable[[Any], Any]]] = {
        "service": service_choice,
        **{key: field[2] for key, field in _SWEEP_FIELDS.items()},
    }

    def __init__(self, resources: ExitStack) -> None:
        self.resources = resources
        self.resolver = ModelServiceResolver()
        self.service = None
        self.selection = None

    def validate_record(self, record: SweepPoint, line_no: int) -> None:
        unknown = set(record) - set(self.axis_fields) - {_BENCHMARK_NAME, _PARAMETER_GROUP}
        if unknown:
            raise ValueError(f"Unsupported sweep keys on line {line_no}: " + ", ".join(sorted(unknown)))
        for key in (_BENCHMARK_NAME, _PARAMETER_GROUP):
            if key in record:
                try:
                    STRING.validate_python(record[key])
                except ValidationError as error:
                    raise ValueError(f"Invalid sweep field {key!r}: {error}") from error

    def apply_point(self, config: BenchmarkConfig, point: SweepPoint) -> BenchmarkConfig:
        """Apply explicit workload axes and the selected service without mutating the base benchmark."""
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
            choice = cast(dict[str, str], point["service"])
            updated = replace(updated, service=select_service(config.service, choice))
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
    ) -> BenchmarkRun:
        """Run one HTTP or SLO point while reusing adjacent service selections owned by the sweep stack."""
        point_config = replace(
            config, sweep=ParameterSweepConfig(),
            wandb=replace(config.wandb, group=wandb_group),
        )
        selection = (config.service, bool(config.trace.trace_selector or config.is_multi_turn))

        def prepare_service() -> ModelService:
            """Reuse the service across adjacent sweep points with the same selection."""
            if selection != self.selection:
                self.resources.close()
                self.service = self.resources.enter_context(self.resolver.benchmark(point_config))
                self.selection = selection
            logger.info("{}", format_benchmark_config(point_config, self.service))
            return self.service

        if point_config.slo.search:
            result = SloAutoTuneBenchmark(
                point_config,
                prepare_service,
                label=label,
                output_dir=output_dir,
            ).run()
        else:
            result = HttpBenchmark(
                point_config,
                prepare_service(),
                label=label,
                output_dir=output_dir,
                wandb_group=wandb_group,
            ).run()
        return result
