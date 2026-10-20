# SPDX-License-Identifier: Apache-2.0
# SPDX-FileCopyrightText: Copyright contributors to the Foretoken project

"""Discover saved result owners without entering nested evaluator or media artifacts."""

from __future__ import annotations

import json
import logging
from collections.abc import Iterator
from contextlib import nullcontext
from dataclasses import dataclass
from pathlib import Path

from benchmarks.results.recording.locks import publication_lock
from benchmarks.results.recording.tasks import task_source

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class SavedResultDirectory:
    """A saved result set and the optional command record that owns its execution."""

    directory: Path
    group: Path
    context: Path | None


def discover_result_directories(
    sources: tuple[Path, ...], *, base: Path | None = None,
) -> list[SavedResultDirectory]:
    """Resolve plotting and publication inputs to their result owners and experiment groups."""
    base = base.resolve() if base is not None else None
    selected = list(dict.fromkeys(path.resolve() for path in ((base, *sources) if base is not None else sources)))
    for path in selected:
        if not path.is_dir():
            raise NotADirectoryError(f"Result directory does not exist: {path}")
    # Keep an explicit base first so it owns shared resumed measurements. Other
    # enclosing selections own traversal; leaf evidence is still counted only once.
    selected = [path for path in selected if path == base or not any(
        path != parent and path.is_relative_to(parent) for parent in selected
    )]
    directories: list[SavedResultDirectory] = []
    for source in selected:
        discovered = list(_discover(source, set()))
        if not discovered:
            raise FileNotFoundError(f"No saved benchmark results in {source}")
        directories.extend(discovered)
    return directories


def result_sources(directory: Path, *others: Path) -> list[Path]:
    """Wait for selected recorders to seal and resolve task owners for plotting and publication."""
    ordered = []
    visited = set()

    def visit(source: Path) -> None:
        if source in visited:
            return
        if not source.is_dir():
            raise ValueError(f"Referenced result directory is missing: {source}; retain the original task results when moving a resumed run")
        visited.add(source)
        owned = (source / "context.json").is_file()
        if owned and json.loads((source / "context.json").read_text()).get("status") == "running":
            logger.info("Waiting for the result recorder to close: %s", source)
        # A running aggregate can create more children. Discover them only after
        # its recorder seals, then release its lock before waiting on any child.
        with publication_lock(source) if owned else nullcontext():
            children = {task_source(path.parent) for path in source.rglob("task.json")}
            children.update(path.parent for path in source.rglob("publication.json"))
            children.update(path.parent for path in source.rglob("wandb.json"))
        for child in sorted(children - {source}):
            visit(child)
        if owned:
            ordered.append(source)

    for selected in discover_result_directories((directory, *others)):
        visit(selected.directory)
    return ordered


def figure_source(directory: Path) -> Path | None:
    """Return the recorded owner of a complete figure export; comparisons have no single run owner."""
    manifest = directory / ".foretoken-plots.json"
    if not manifest.is_file():
        return None
    record = json.loads(manifest.read_text(encoding="utf-8"))
    source = record.get("source") if isinstance(record, dict) else None
    return (directory / source).resolve() if source is not None else None


def _parent_plan(directory: Path) -> tuple[Path, str] | None:
    """Locate an enclosing result plan without crossing its command boundary."""
    for parent in directory.parents:
        if (parent / "generated/context.json").is_file():
            break
        configuration = parent / "config.json"
        config = json.loads(configuration.read_text()) if configuration.is_file() else {}
        if config.get("mode") in {"parameter_sweep", "video_parameter_sweep"}:
            return parent, "sweep"
        metrics_path = parent / "metrics.json"
        metrics = json.loads(metrics_path.read_text()) if metrics_path.is_file() else {}
        if "evaluation_comparison" in metrics:
            return parent, "evaluation"
        if config.get("mode") == "slo_search" or (parent / "slo_results.json").is_file():
            return parent, "slo_search"
    return None


def _enclosing_context(directory: Path) -> Path | None:
    """Find a command record when a user selects one of its result directories directly."""
    return next((path for parent in (directory, *directory.parents)
                 if (path := parent / "generated/context.json").is_file()), None)


def _discover(
    directory: Path, visited: set[Path], *, group: Path | None = None,
    context: Path | None = None,
) -> Iterator[SavedResultDirectory]:
    """Descend directory containers, stopping at result owners before their native children."""
    directory = directory.resolve()
    group = group.resolve() if group is not None else None
    if directory in visited:
        return
    visited.add(directory)
    command = directory / "generated/context.json"
    if command.is_file():
        found = False
        for child in sorted((directory / "artifacts").glob("*")):
            if child.is_dir():
                for result in _discover(child, visited, group=group or directory, context=command):
                    found = True
                    yield result
        if not found:
            yield SavedResultDirectory(directory, group or directory, command)
        return
    if any((directory / name).is_file() for name in ("config.json", "slo_results.json", "task.json", "context.json")):
        yield SavedResultDirectory(directory, group or directory, context or _enclosing_context(directory))
        return
    if (directory / "iterations").is_dir():
        for child in sorted((directory / "iterations").iterdir()):
            if child.is_dir():
                yield from _discover(child, visited, group=child)
        return
    if (directory / "runs").is_dir():
        for child in sorted((directory / "runs").iterdir()):
            if child.is_dir():
                yield from _discover(child, visited, group=group or directory)
        return
    parent = _parent_plan(directory)
    if parent is not None and parent[1] != "slo_search":
        # A selected sweep point still owns its planned repetitions, including
        # children that have not created their directories yet.
        yield SavedResultDirectory(directory, group or directory, context or _enclosing_context(directory))
        return
    for child in sorted(directory.iterdir()):
        if child.is_dir() and not child.name.startswith("."):
            yield from _discover(child, visited, group=group, context=context)
