"""Apply bench-params JSON overrides onto ``BenchArguments``."""

from __future__ import annotations

from copy import deepcopy
from dataclasses import fields, replace
from typing import Any

from benchmarks.arguments import BenchArguments, parse_float_list, parse_int_list, parse_str_list
from benchmarks.sweep.param_sweep import ParameterSweepItem

# vLLM / common aliases → BenchArguments field names (ingest only).
# Metrics and config emit canonical names: parallel / number / ...
_BENCH_KEY_ALIASES: dict[str, str] = {
    "max_concurrency": "parallel",
    "concurrency": "parallel",  # input alias only; do not emit on metrics
    "num_prompts": "number",
    "max-tokens": "max_tokens",
    "dataset-path": "dataset_path",
    "datasets": "dataset_path",  # legacy multi-path key → dataset_path list
    "dataset-offset": "dataset_offset",
    "tokenizer-path": "tokenizer_path",
    "min-prompt-length": "min_prompt_length",
    "max-prompt-length": "max_prompt_length",
    "prefix-length": "prefix_length",
    "apply-chat-template": "apply_chat_template",
    "max-turns": "max_turns",
    "gpu-count": "gpu_count",
    "outputs-dir": "outputs_dir",
    "api-key": "api_key",
    "engine-metrics-url": "engine_metrics_url",
    "engine-metrics-interval": "engine_metrics_interval",
    "gpu-metrics-interval": "engine_metrics_interval",  # deprecated alias
    "gpu_metrics_interval": "engine_metrics_interval",
}


def _canonical_bench_key(key: str) -> str:
    if key == "_benchmark_name":
        return key
    norm = key.replace("-", "_")
    return _BENCH_KEY_ALIASES.get(key) or _BENCH_KEY_ALIASES.get(norm) or norm


def _coerce_field(name: str, value: Any) -> Any:
    if name in ("parallel", "number"):
        if isinstance(value, list):
            return [int(x) for x in value]
        if isinstance(value, int):
            return [int(value)]
        return parse_int_list(str(value))
    if name == "rate":
        if isinstance(value, list):
            return [float(x) for x in value]
        if isinstance(value, (int, float)):
            return [float(value)]
        return parse_float_list(str(value)) if str(value).strip() else [-1.0]
    if name == "dataset_path":
        # Legacy bench-params key ``datasets`` is aliased to ``dataset_path``.
        if isinstance(value, list):
            return [str(x) for x in value if str(x).strip()]
        if value is None or value == "":
            return []
        return parse_str_list(str(value))
    if name in (
        "stream",
        "sla_auto_tune",
        "wandb",
        "open_loop",
        "collect_engine_metrics",
        "dry_run",
    ):
        if isinstance(value, str):
            return value.strip().lower() in ("1", "true", "yes", "on")
        return bool(value)
    if name == "apply_chat_template":
        if value is None:
            return None
        if isinstance(value, str):
            lowered = value.strip().lower()
            if lowered in ("", "none", "auto"):
                return None
            return lowered in ("1", "true", "yes", "on")
        return bool(value)
    if name in (
        "timeout",
        "max_tokens",
        "dataset_offset",
        "min_prompt_length",
        "max_prompt_length",
        "prefix_length",
        "gpu_count",
        "num_runs",
    ):
        return int(value)
    if name in ("temperature", "engine_metrics_interval", "gpu_metrics_interval"):
        return float(value)
    if name == "max_turns":
        return None if value is None else int(value)
    return value


_FIELD_NAMES = {f.name for f in fields(BenchArguments)}


def apply_bench_overrides(
    base: BenchArguments,
    overrides: ParameterSweepItem | dict[str, object],
) -> BenchArguments:
    """Return a copy of ``base`` with bench-params overrides applied."""
    updates: dict[str, Any] = {}
    for raw_key, raw_value in dict(overrides).items():
        if raw_key == "_benchmark_name":
            continue
        key = _canonical_bench_key(str(raw_key))
        if key not in _FIELD_NAMES:
            # Keep unknown keys out of BenchArguments; still stored on run meta.
            continue
        updates[key] = _coerce_field(key, raw_value)
    if not updates:
        return deepcopy(base)
    return replace(base, **updates)
