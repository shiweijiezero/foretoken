# SPDX-License-Identifier: Apache-2.0
# SPDX-FileCopyrightText: Copyright contributors to the Foretoken project

"""Apply AgentX replay choices through the shared sweep and service lifecycles."""

from __future__ import annotations

from collections.abc import Callable
from contextlib import ExitStack
from dataclasses import replace
from typing import Any, ClassVar, cast

from pydantic import ConfigDict, TypeAdapter, ValidationError

from benchmarks.config.agentx import AgentXConfig
from benchmarks.config.benchmark import (
    ModelServiceSource,
    ParameterSweepConfig,
    parse_duration_seconds,
    select_service,
    service_choice,
)
from benchmarks.config.decoding import BOOLEAN, INTEGER, NUMBER, STRING
from benchmarks.model_service import ModelService, ModelServiceResolver
from benchmarks.results.types import BenchmarkRun
from benchmarks.results.wandb.recording import wandb_run_timestamp
from benchmarks.sweeps.core import (
    _BENCHMARK_NAME,
    _PARAMETER_GROUP,
    SweepAdapter,
    SweepDefinition,
    SweepPoint,
    load_sweep_points,
    run_sweep,
)

_EXTRA_BODY = TypeAdapter(dict[str, Any], config=ConfigDict(strict=True, hide_input_in_errors=True))
_REPLAY_FIELDS: dict[str, Callable[[Any], Any]] = {
    "max_concurrency": INTEGER.validate_python,
    "duration": parse_duration_seconds,
    "max_context_length": INTEGER.validate_python,
    "dataset": STRING.validate_python,
    "input_file": STRING.validate_python,
    "tokenizer_path": STRING.validate_python,
    "tokenizer_trust_remote_code": BOOLEAN.validate_python,
    "random_seed": INTEGER.validate_python,
    "warmup_requests_per_lane": INTEGER.validate_python,
    "agentic_warmup_grace_period": parse_duration_seconds,
    "benchmark_grace_period": parse_duration_seconds,
    "request_timeout_seconds": parse_duration_seconds,
    "record_processors": INTEGER.validate_python,
    "trajectory_start_min_ratio": NUMBER.validate_python,
    "trajectory_start_max_ratio": NUMBER.validate_python,
    "unsafe_override": BOOLEAN.validate_python,
    "extra_body": _EXTRA_BODY.validate_python,
}


def run_agentx_sweep(config: AgentXConfig) -> BenchmarkRun:
    """Measure AgentX repetitions and services while the common core owns progress and recovery."""
    resources = ExitStack()
    adapter = _AgentXSweepAdapter(resources)
    sweep = config.sweep
    definition = SweepDefinition(sweep.path, sweep.num_runs, sweep.experiment_name, sweep.resume)
    if sweep.path:
        points = load_sweep_points(definition, adapter)
    else:
        points = [{_BENCHMARK_NAME: "agentx", _PARAMETER_GROUP: "agentx"}]
    choices = [
        {"name": source.name or "default", "path" if source.kustomize_path else "url": source.kustomize_path or source.url,
         "model": source.model, "health_url": source.health_url}
        for source in config.services
    ]
    if len(choices) > 1:
        points = [{**point, "service": choice}
                  for point in points
                  for choice in ([point["service"]] if "service" in point else choices)]
    methods: dict[str, ModelServiceSource] = {}
    for point in points:
        source = adapter.apply_point(config, point).service
        if source.name in methods and methods[source.name] != source:
            raise ValueError(f"Sweep service name {source.name!r} refers to different services")
        methods[source.name] = source
    order = {name: index for index, name in enumerate(methods)}
    points.sort(key=lambda point: order[adapter.apply_point(config, point).service.name])
    return run_sweep(
        config, definition, adapter, mode="parameter_sweep",
        combinations=points, service_resources=resources,
    ).run


class _AgentXSweepAdapter(SweepAdapter[AgentXConfig]):
    """Own service reuse and native replay choices while borrowing common sweep orchestration."""

    axis_fields: ClassVar[dict[str, Callable[[Any], Any]]] = {"service": service_choice, **_REPLAY_FIELDS}

    def __init__(self, resources: ExitStack) -> None:
        self.resources = resources
        self.resolver = ModelServiceResolver()
        self.service: ModelService | None = None
        self.selection: ModelServiceSource | None = None

    def validate_record(self, record: SweepPoint, line_no: int) -> None:
        """Check explicit AgentX axes before expanding a JSONL parameter row."""
        unknown = set(record) - set(self.axis_fields) - {_BENCHMARK_NAME, _PARAMETER_GROUP}
        if unknown:
            raise ValueError(f"Unsupported AgentX sweep keys on line {line_no}: " + ", ".join(sorted(unknown)))
        if "dataset" in record and "input_file" in record:
            raise ValueError("AgentX sweep rows select either dataset or input_file")
        for key in (_BENCHMARK_NAME, _PARAMETER_GROUP):
            if key in record:
                try:
                    STRING.validate_python(record[key])
                except ValidationError as error:
                    raise ValueError(f"Invalid sweep field {key!r}: {error}") from error

    def apply_point(self, config: AgentXConfig, point: SweepPoint) -> AgentXConfig:
        """Apply replay axes and one named service without changing the base configuration."""
        updates = {key: point[key] for key in _REPLAY_FIELDS if key in point}
        if "dataset" in updates:
            updates["input_file"] = ""
        elif "input_file" in updates:
            updates["dataset"] = ""
        source = select_service(config.service, cast(dict[str, str], point["service"])) if "service" in point else config.service
        return replace(config, services=(source,), **updates)

    def validate_point(self, config: AgentXConfig) -> None:
        """Validate the selected source and replay configuration before any deployment starts."""
        config.validate()

    def plan_base(self, config: AgentXConfig) -> dict[str, Any]:
        """Persist the same public replay configuration used by standalone runs."""
        return config.to_dict()

    def group_name(self, config: AgentXConfig) -> str:
        """Use one W&B group across all measured replay configurations."""
        return config.wandb.group.strip() or f"{config.sweep.experiment_name or 'agentx'}_{wandb_run_timestamp()}"

    def execute_point(
        self, config: AgentXConfig, *, output_dir: str, label: str,
        wandb_group: str, dry_run: bool,
    ) -> BenchmarkRun:
        """Run one native replay while retaining adjacent selections in the sweep's resource stack."""
        from benchmarks.runs.agentx import run_agentx

        point_config = replace(config, sweep=ParameterSweepConfig(), wandb=replace(config.wandb, group=wandb_group))
        if config.service != self.selection:
            self.resources.close()
            self.service = self.resources.enter_context(self.resolver.resolve(config.service))
            self.selection = config.service
        assert self.service is not None
        return run_agentx(point_config, self.service, output_dir=output_dir, label=label, wandb_group=wandb_group)
