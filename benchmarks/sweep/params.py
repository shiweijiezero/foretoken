# SPDX-License-Identifier: Apache-2.0
# SPDX-FileCopyrightText: Copyright contributors to the Foretoken project


"""Bench parameter sweep: load JSON and apply overrides onto ``BenchConfig``."""

from __future__ import annotations

from dataclasses import fields, replace
from typing import Any

from vllm.benchmarks.sweep.param_sweep import ParameterSweep, ParameterSweepItem
from vllm.benchmarks.sweep.utils import sanitize_filename

from benchmarks.config import (
    BenchConfig,
    DatasetConfig,
    EngineMetricsConfig,
    GenerationConfig,
    LoadConfig,
    OutputConfig,
    TargetConfig,
    WandbConfig,
)

__all__ = [
    "ParameterSweep",
    "ParameterSweepItem",
    "sanitize_filename",
    "load_param_sweep",
    "apply_bench_overrides",
]


def load_param_sweep(path: str) -> ParameterSweep:
    """Load a bench-params JSON file."""
    if not path:
        raise ValueError("--bench-params path is required for param sweep")
    return ParameterSweep.read_json(path)


_SECTION_BY_FIELD: dict[str, str] = {}
for _section, _cls in (
    ("target", TargetConfig),
    ("load", LoadConfig),
    ("generation", GenerationConfig),
    ("dataset", DatasetConfig),
    ("output", OutputConfig),
    ("wandb", WandbConfig),
    ("engine", EngineMetricsConfig),
):
    for _field in fields(_cls):
        # Target.url owns the plain ``url`` key; engine.url is not overridable
        # via flat bench-params (set via CLI / base config instead).
        if _section == "engine" and _field.name == "url":
            continue
        if _field.name in _SECTION_BY_FIELD:
            raise RuntimeError(
                f"duplicate BenchConfig field name {_field.name!r}"
            )
        _SECTION_BY_FIELD[_field.name] = _section


def _resolve_override_target(raw_key: str) -> tuple[str, str]:
    if raw_key in _SECTION_BY_FIELD:
        return _SECTION_BY_FIELD[raw_key], raw_key
    raise ValueError(f"Unknown bench-params key {raw_key!r}")


def _coerce_bool(value: Any) -> bool:
    if isinstance(value, str):
        return value.strip().lower() in ("1", "true", "yes", "on")
    return bool(value)


def _coerce_field(name: str, value: Any) -> Any:
    if name in ("parallel", "number"):
        if isinstance(value, list):
            return [int(item) for item in value]
        return [int(value)]
    if name == "rate":
        if isinstance(value, list):
            return [float(item) for item in value]
        return [float(value)]
    if name == "dataset":
        if isinstance(value, list):
            return [str(item) for item in value]
        return [str(value)]
    if name in (
        "stream",
        "open_loop",
        "sla_auto_tune",
        "enabled",
        "collect",
    ):
        return _coerce_bool(value)
    if name == "apply_chat_template":
        if value is None:
            return None
        if isinstance(value, str):
            lowered = value.strip().lower()
            if lowered in ("", "none", "auto"):
                return None
            return _coerce_bool(lowered)
        return bool(value)
    if name in (
        "timeout",
        "max_retries",
        "max_tokens",
        "dataset_offset",
        "min_prompt_length",
        "max_prompt_length",
        "prefix_length",
        "gpu_count",
    ):
        return int(value)
    if name in ("temperature", "interval"):
        return float(value)
    if name == "max_turns":
        return None if value is None else int(value)
    return value


def apply_bench_overrides(
    config: BenchConfig,
    overrides: ParameterSweepItem | dict[str, object],
) -> BenchConfig:
    """Return a copy of ``config`` with bench-params overrides applied."""
    section_updates: dict[str, dict[str, Any]] = {
        "target": {},
        "load": {},
        "generation": {},
        "dataset": {},
        "output": {},
        "wandb": {},
        "engine": {},
    }
    for raw_key, raw_value in dict(overrides).items():
        if raw_key == "_benchmark_name":
            continue
        section, field_name = _resolve_override_target(str(raw_key))
        section_updates[section][field_name] = _coerce_field(
            field_name, raw_value
        )

    updated = config
    for section, updates in section_updates.items():
        if not updates:
            continue
        nested = getattr(updated, section)
        updated = replace(updated, **{section: replace(nested, **updates)})
    return updated
