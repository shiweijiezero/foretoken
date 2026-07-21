"""Parameter sweep JSON loader.

Prefer vLLM's implementation when installed; otherwise use a compatible
local copy of the list/dict JSON formats.
"""

from __future__ import annotations

import json
import os
from typing import Any, Optional


class ParameterSweepItem(dict[str, object]):
    """One parameter combination (same shape as vLLM)."""

    @classmethod
    def from_record(cls, record: dict[str, object]) -> "ParameterSweepItem":
        if not isinstance(record, dict):
            raise TypeError(
                "Each item in the parameter sweep should be a dictionary, "
                f"but found type: {type(record)}"
            )
        return cls(record)

    @property
    def name(self) -> str:
        if "_benchmark_name" in self:
            return str(self["_benchmark_name"])
        return self.as_text(sep="-")

    def _iter_param_key_candidates(self, param_key: str):
        if "." in param_key:
            prefix, rest = param_key.split(".", 1)
            for prefix_candidate in self._iter_param_key_candidates(prefix):
                yield prefix_candidate + "." + rest
            return
        yield param_key
        yield param_key.replace("-", "_")
        yield param_key.replace("_", "-")

    def has_param(self, param_key: str) -> bool:
        return any(k in self for k in self._iter_param_key_candidates(param_key))

    def get_param(self, param_key: str, default: Any = None) -> Any:
        for key in self._iter_param_key_candidates(param_key):
            if key in self:
                return self[key]
        return default

    def as_text(self, sep: str = ", ") -> str:
        return sep.join(
            f"{k}={v}" for k, v in self.items() if k != "_benchmark_name"
        )


class ParameterSweep(list[ParameterSweepItem]):
    @classmethod
    def read_json(cls, filepath: os.PathLike) -> "ParameterSweep":
        # Always normalize into local items (vLLM JSON formats are compatible).
        with open(filepath, "rb") as f:
            data = json.load(f)
        if isinstance(data, dict):
            return cls.read_from_dict(data)
        return cls.from_records(data)

    @classmethod
    def read_from_dict(cls, data: dict[str, dict[str, object]]) -> "ParameterSweep":
        records = [
            {"_benchmark_name": name, **params} for name, params in data.items()
        ]
        return cls.from_records(records)

    @classmethod
    def from_records(cls, records: list[dict[str, object]]) -> "ParameterSweep":
        if not isinstance(records, list):
            raise TypeError(
                "The parameter sweep should be a list of dictionaries, "
                f"but found type: {type(records)}"
            )
        names = [r["_benchmark_name"] for r in records if "_benchmark_name" in r]
        if names and len(names) != len(set(names)):
            duplicates = [name for name in names if names.count(name) > 1]
            raise ValueError(
                f"Duplicate _benchmark_name values found: {set(duplicates)}. "
                "All _benchmark_name values must be unique."
            )
        return cls(ParameterSweepItem.from_record(record) for record in records)

    @classmethod
    def empty_default(cls) -> "ParameterSweep":
        """Single empty override — run base command unchanged (vLLM default)."""
        return cls.from_records([{}])


def load_param_sweep(path: Optional[str]) -> ParameterSweep:
    if not path:
        return ParameterSweep.empty_default()
    return ParameterSweep.read_json(path)


def parse_link_vars(value: str) -> list[tuple[str, str]]:
    """Parse ``serve_key=bench_key,...`` like vLLM ``--link-vars``."""
    if not value or not value.strip():
        return []
    pairs: list[tuple[str, str]] = []
    for item in value.split(","):
        item = item.strip()
        if not item:
            continue
        if "=" not in item:
            raise ValueError(
                f"Invalid --link-vars item {item!r}; expected serve_key=bench_key"
            )
        left, right = item.split("=", 1)
        pairs.append((left.strip(), right.strip()))
    return pairs


def comb_is_valid(
    serve_comb: ParameterSweepItem,
    bench_comb: ParameterSweepItem,
    link_vars: list[tuple[str, str]],
) -> bool:
    for serve_key, bench_key in link_vars:
        if not serve_comb.has_param(serve_key):
            return False
        bench_val = _bench_link_param(bench_comb, bench_key)
        if bench_val is _MISSING:
            return False
        if serve_comb.get_param(serve_key) != bench_val:
            return False
    return True


_MISSING = object()


def _bench_link_param(bench_comb: ParameterSweepItem, bench_key: str) -> Any:
    """Resolve a --link-vars bench key through CLI aliases.

    e.g. ``max_num_seqs=parallel`` matches bench JSON ``max_concurrency``.
    """
    from benchmarks.sweep.overrides import _BENCH_KEY_ALIASES, _canonical_bench_key

    canonical = _canonical_bench_key(bench_key)
    candidates = {
        bench_key,
        canonical,
        bench_key.replace("-", "_"),
        bench_key.replace("_", "-"),
        canonical.replace("_", "-"),
    }
    for alias, target in _BENCH_KEY_ALIASES.items():
        if target == canonical:
            candidates.add(alias)
            candidates.add(alias.replace("-", "_"))
            candidates.add(alias.replace("_", "-"))
    for key in candidates:
        if bench_comb.has_param(key):
            return bench_comb.get_param(key)
    return _MISSING


def sanitize_filename(filename: str) -> str:
    return filename.replace("/", "_").replace("..", "__").strip("'").strip('"')
