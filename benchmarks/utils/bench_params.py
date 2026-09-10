# SPDX-License-Identifier: Apache-2.0
# SPDX-FileCopyrightText: Copyright contributors to the Foretoken project


"""Parse ``--bench-params`` JSONL and apply overrides onto ``BenchConfig``."""

from __future__ import annotations

from dataclasses import fields, replace
from typing import Any, Callable

from evalscope.perf.arguments import Arguments
from vllm.benchmarks.sweep.param_sweep import ParameterSweep, ParameterSweepItem
from vllm.benchmarks.sweep.utils import sanitize_filename

from benchmarks.config import (
    BenchConfig,
    DatasetConfig,
    EndpointConfig,
    GenerationConfig,
    LoadConfig,
)
from benchmarks.workload.loader import load_jsonl

__all__ = [
    "ParameterSweep",
    "ParameterSweepItem",
    "sanitize_filename",
    "load_param_sweep",
    "expand_load_points",
    "apply_bench_overrides",
]

_LOAD_CAST = {"parallel": int, "number": int, "rate": float}


def _as_dataset(value: Any) -> list[str]:
    if isinstance(value, list):
        return [str(item) for item in value]
    return [str(value)]


def _identity(value: Any) -> Any:
    return value


# Flat bench-params key -> (BenchConfig section, coerce).
# Built from nested dataclasses; skip fields that must not be swept.
_COERCE: dict[str, Callable[[Any], Any]] = {
    "headers": dict,
    "extra_body": dict,
    "dataset": _as_dataset,
    "max_tokens": Arguments._validate_max_tokens,
}
_SKIP_FIELDS = frozenset(
    {
        "max_turns",
        "api_key",
        "headers",
        "trace_path",
        "trace_start",
        "trace_duration",
        "trace_max_concurrency",
        "trace_synthetic_prefix_reuse",
    }
)
_BENCH_PARAM_FIELDS: dict[str, tuple[str, Callable[[Any], Any]]] = {
    f.name: (section, _COERCE.get(f.name, _identity))
    for section, cls in (
        ("endpoint", EndpointConfig),
        ("load", LoadConfig),
        ("generation", GenerationConfig),
        ("dataset", DatasetConfig),
    )
    for f in fields(cls)
    if f.name not in _SKIP_FIELDS
}


def load_param_sweep(path: str) -> ParameterSweep:
    """Load a bench-params JSONL file (one JSON object per line)."""
    if not path:
        raise ValueError("Parameter sweep requires --bench-params PATH")

    points: list[dict[str, object]] = []
    for line_no, record in load_jsonl(path, allow_comments=True):
        if not isinstance(record, dict):
            raise TypeError(
                "Each bench-params JSONL line must be an object, "
                f"got {type(record)} on line {line_no}"
            )
        points.extend(dict(point) for point in expand_load_points(record))
    return ParameterSweep.from_records(points)


def _axis(record: dict[str, object], key: str, caster: type) -> list[Any] | None:
    if key not in record:
        return None
    value = record[key]
    if isinstance(value, list):
        return [caster(item) for item in value]
    return [caster(value)]


def _pick(values: list[Any] | None, index: int) -> Any | None:
    if values is None:
        return None
    return values[index] if len(values) > 1 else values[0]


def expand_load_points(
    item: ParameterSweepItem | dict[str, object],
) -> list[ParameterSweepItem]:
    """Expand list-valued load keys into scalar bench-params items."""
    record = dict(item)
    axes = {key: _axis(record, key, caster) for key, caster in _LOAD_CAST.items()}
    multi = {
        key: values
        for key, values in axes.items()
        if values is not None and len(values) > 1
    }

    if "rate" in multi and "parallel" in multi:
        raise ValueError(
            "Cannot sweep both rate and parallel in one bench-params line; "
            "pass one multi-value list at a time."
        )

    primary = next(
        (key for key in ("rate", "parallel", "number") if key in multi),
        None,
    )
    count = len(multi[primary]) if primary else 1
    if (
        primary in ("rate", "parallel")
        and "number" in multi
        and len(multi["number"]) != count
    ):
        raise ValueError(
            f"number list must match {primary} length when both are "
            f"multi-value; got number={len(multi['number'])}, {primary}={count}"
        )

    base_name = record.get("_benchmark_name")
    rest = {
        key: value
        for key, value in record.items()
        if key not in (*_LOAD_CAST, "_benchmark_name", "_parameter_group")
    }
    parameter_group = (
        str(base_name)
        if base_name is not None
        else ParameterSweepItem(rest).name or "default"
    )

    results: list[ParameterSweepItem] = []
    for index in range(count):
        point = {
            **rest,
            **{
                key: _pick(values, index)
                for key, values in axes.items()
                if values is not None
            },
        }
        parallel = point.get("parallel")
        rate = point.get("rate")
        number = point.get("number")
        if parallel is not None or rate is not None:
            LoadConfig.validate_point(
                parallel=int(parallel) if parallel is not None else 1,
                rate=float(rate) if rate is not None else -1.0,
            )
        if number is not None and int(number) < 1:
            raise ValueError(f"number must be >= 1, got {number}")

        point["_parameter_group"] = parameter_group
        if base_name is not None:
            if count > 1:
                rate_value = point.get("rate")
                if rate_value is None:
                    r_tag = "x"
                elif float(rate_value) == -1:
                    r_tag = "-1"
                else:
                    r_tag = f"{float(rate_value):g}"
                point["_benchmark_name"] = (
                    f"{base_name}"
                    f"-p{point.get('parallel', 'x')}"
                    f"-n{point.get('number', 'x')}"
                    f"-r{r_tag}"
                )
            else:
                point["_benchmark_name"] = str(base_name)
        results.append(ParameterSweepItem(point))
    return results


def apply_bench_overrides(
    config: BenchConfig,
    overrides: ParameterSweepItem | dict[str, object],
) -> BenchConfig:
    """Return a copy of ``config`` with bench-params overrides applied."""
    section_updates: dict[str, dict[str, Any]] = {}
    for raw_key, raw_value in dict(overrides).items():
        if raw_key in {"_benchmark_name", "_parameter_group"}:
            continue
        field = _BENCH_PARAM_FIELDS.get(str(raw_key))
        if field is None:
            allowed = ", ".join(sorted(_BENCH_PARAM_FIELDS))
            raise ValueError(
                f"Unsupported bench-params key {raw_key!r}. "
                "Only fields that change request execution may be swept; "
                f"allowed keys: {allowed}"
            )
        section, coerce = field
        section_updates.setdefault(section, {})[str(raw_key)] = coerce(raw_value)

    updated = config
    for section, updates in section_updates.items():
        nested = getattr(updated, section)
        updated = replace(updated, **{section: replace(nested, **updates)})
    return updated
