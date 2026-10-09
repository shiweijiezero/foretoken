# SPDX-License-Identifier: Apache-2.0
# SPDX-FileCopyrightText: Copyright contributors to the Foretoken project

"""Keep command-level experiment notes and checkout changes beside benchmark results."""

from __future__ import annotations

import json
import logging
import os
import shutil
import stat
import subprocess
import time
from collections.abc import Iterator, Sequence
from contextlib import contextmanager
from dataclasses import replace
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, TypeVar
from urllib.parse import urlsplit, urlunsplit

from benchmarks.config.benchmark import BenchmarkConfig
from benchmarks.config.evaluation import EvaluationConfig
from benchmarks.config.video import VideoBenchmarkConfig
from benchmarks.results.console import capture_run_logs

logger = logging.getLogger(__name__)
_Config = TypeVar("_Config", BenchmarkConfig, EvaluationConfig, VideoBenchmarkConfig)


def _numbered_directory(parent: Path, prefix: str = "") -> Path:
    """Reserve the next directory without sharing it with concurrent commands."""
    parent.mkdir(parents=True, exist_ok=True)
    numbers = [
        int(name) for child in parent.iterdir()
        if child.name.startswith(prefix) and (name := child.name[len(prefix):]).isdigit()
    ]
    number = max(numbers, default=0) + 1
    while True:
        directory = parent / f"{prefix}{number:06d}"
        try:
            directory.mkdir()
        except FileExistsError:
            number += 1
        else:
            return directory


def _create_note(path: Path, text: str) -> None:
    """Create a blank note template once; later runs leave its contents to the author."""
    path.parent.mkdir(parents=True, exist_ok=True)
    try:
        with path.open("x", encoding="utf-8") as handle:
            handle.write(text)
    except FileExistsError:
        pass


def _git(root: Path, *arguments: str) -> bytes:
    """Read Git metadata without changing the checkout or index."""
    return subprocess.check_output(["git", "-C", str(root), *arguments])


def _snapshot_repository(root: Path, destination: Path, output_root: Path) -> dict[str, Any]:
    """Save final working-tree contents relative to HEAD, including initialized submodules."""
    head = subprocess.run(
        ["git", "-C", str(root), "rev-parse", "--verify", "--quiet", "HEAD"],
        capture_output=True, check=False,
    )
    if head.returncode not in (0, 1):
        head.check_returncode()
    revision = head.stdout.decode().strip() or None
    # No rename detection is needed for restoration: a rename is a deletion and
    # an addition. NUL-delimited paths preserve spaces, newlines and non-ASCII names.
    tracked = (
        _git(root, "diff", "--name-only", "--no-renames", "-z", "HEAD", "--")
        if revision else _git(root, "ls-files", "-z")
    )
    untracked = _git(root, "ls-files", "--others", "--exclude-standard", "-z")
    paths = sorted({os.fsdecode(path) for path in (tracked + untracked).split(b"\0") if path})
    entries: list[dict[str, Any]] = []
    retained_repositories: set[str] = set()
    for name in paths:
        source = root / name
        # Results may be outside the repository's ignore rules. Never snapshot
        # the running experiment itself, or dereference a changed symlink.
        if output_root == root or root.is_relative_to(output_root):
            if any(source.is_relative_to(output_root / name) for name in ("iterations", "notes")):
                continue
        elif source.is_relative_to(output_root):
            continue
        entry: dict[str, Any] = {"path": name}
        entries.append(entry)
        try:
            mode = source.lstat().st_mode
        except FileNotFoundError:
            entry["action"] = "delete"
            continue
        entry["mode"] = stat.S_IMODE(mode)
        if stat.S_ISLNK(mode):
            entry.update(action="symlink", target=os.readlink(source))
        elif stat.S_ISREG(mode):
            target = destination / name
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(source, target)
            entry["action"] = "replace"
        elif stat.S_ISDIR(mode):
            entry["action"] = "directory"
            if (source / ".git").exists():
                retained_repositories.add(name)
        else:
            entry["action"] = "unsupported"

    # Each initialized submodule has its own HEAD and uncommitted changes. Keep
    # that identity even when its parent Git link still points at an older commit.
    submodules = []
    indexed_submodules: dict[str, str] = {}
    for raw in _git(root, "ls-files", "--stage", "-z").split(b"\0"):
        if not raw:
            continue
        metadata, path = raw.split(b"\t", 1)
        if metadata.split()[0] != b"160000":
            continue
        name = os.fsdecode(path)
        indexed_submodules[name] = metadata.split()[1].decode()
    for name in sorted(indexed_submodules.keys() | retained_repositories):
        if (root / name / ".git").exists():
            submodules.append({"path": name, **_snapshot_repository(root / name, destination / name, output_root)})
        else:
            submodules.append({"path": name, "commit": indexed_submodules[name], "initialized": False})
    return {"commit": revision, "entries": entries, "submodules": submodules}


def capture_changes(destination: Path, output_root: Path) -> dict[str, Any]:
    """Snapshot the invoking checkout; this is not a claim about the serving image's source."""
    if shutil.which("git") is None:
        return {"status": "unavailable", "reason": "Git is not installed"}
    result = subprocess.run(
        ["git", "rev-parse", "--show-toplevel"], capture_output=True, text=True, check=False,
    )
    if result.returncode:
        return {"status": "unavailable", "reason": result.stderr.strip()}
    root = Path(result.stdout.strip()).resolve()
    destination.mkdir(parents=True, exist_ok=True)
    return {"status": "captured", "root": str(root), **_snapshot_repository(root, destination, output_root)}


def _command_arguments(arguments: Sequence[str]) -> list[str]:
    """Retain command options while omitting credential and opaque native argument values."""
    # Native evaluator dictionaries can contain provider-specific credentials.
    # Their safe resolved settings are already emitted by the evaluation adapter.
    private = {
        "--api-key", "--api_key", "--reference-api-key", "--token", "--auth-token", "--auth_token",
        "--password", "--headers", "--header", "--model_args", "--model-args",
        "--extra-body", "--dataset-args", "--dataset_args", "--judge-model-args",
        "--judge_model_args", "--hf-token", "--hf_token",
    }
    result = []
    redact = False
    for argument in arguments:
        name, separator, _ = argument.partition("=")
        if name.startswith("--"):
            redact = False
        if name.startswith("--") and any(option.startswith(name) for option in private):
            result.append(name + "=<redacted>" if separator else name)
            redact = True
        elif redact:
            result.append("<redacted>")
        else:
            value = argument.partition("=")[2] if separator and name.startswith("--") else argument
            if "://" in value:
                try:
                    url = urlsplit(value)
                    value = urlunsplit((url.scheme, url.netloc.rsplit("@", 1)[-1], url.path, "", ""))
                except ValueError:
                    # Prompt text can resemble an invalid URL; recording must still work.
                    value = "<redacted-url>"
                argument = f"{name}={value}" if separator and name.startswith("--") else value
            result.append(argument)
    return result


@contextmanager
def experiment_output(config: _Config, command: str, arguments: Sequence[str]) -> Iterator[_Config]:
    """Wrap one CLI invocation, retaining failures and delegating all measurements to existing runners."""
    outputs = config.outputs
    if not outputs.includes("experiment"):
        yield config
        return
    root = Path(outputs.output_dir).resolve()
    iterations = root / "iterations"
    if outputs.iteration:
        iteration = iterations / outputs.iteration
        iteration.mkdir(parents=True, exist_ok=True)
    else:
        iteration = _numbered_directory(iterations)
    run = _numbered_directory(iteration / "runs", f"{command}-")
    _create_note(
        root / "notes" / "experiment.md",
        "# Experiment Notes\n\n## Goal\n\n## Scope\n\n## Comparison\n\n## Findings\n\n## Next questions\n",
    )
    _create_note(
        iteration / "notes" / "iteration.md",
        "# Iteration Notes\n\n## Question and analysis\n\n## Hypothesis and design\n\n"
        "## Implementation and deployment\n\n## Measurements and interpretation\n\n"
        "## Time spent\n\n## Decision and completion\n\n## Optional behavior and attribution review\n",
    )
    generated = run / "generated"
    generated.mkdir()
    started = time.monotonic()
    context: dict[str, Any] = {
        "command": ["foretoken", *command.split("-"), *_command_arguments(arguments)],
        "started_at": datetime.now(UTC).isoformat(),
        "status": "running",
    }

    def write_context() -> None:
        (generated / "context.json").write_text(json.dumps(context, indent=2, ensure_ascii=True) + "\n", encoding="utf-8")

    write_context()
    logger.info("Experiment run: %s", run)
    # Remove only this output decorator. All nested sweeps, SLO probes and
    # comparisons keep their existing result paths below this invocation.
    run_outputs = replace(
        outputs,
        destinations=tuple(dict.fromkeys("local" if item == "experiment" else item for item in outputs.destinations)),
        output_dir=str(run / "artifacts"),
        iteration="",
    )
    try:
        with capture_run_logs(str(generated), quiet=outputs.includes("quiet")):
            capture_started = time.monotonic()
            context["checkout"] = capture_changes(generated / "changes", root)
            context["capture_seconds"] = time.monotonic() - capture_started
            write_context()
            yield replace(config, outputs=run_outputs)
    except BaseException as error:
        context["status"] = "interrupted" if isinstance(error, KeyboardInterrupt) else "failed"
        context["exit_code"] = (error.code if isinstance(error, SystemExit) and isinstance(error.code, int) else 130 if isinstance(error, KeyboardInterrupt) else 1)
        context["error_type"] = type(error).__name__
        raise
    else:
        context.update(status="completed", exit_code=0)
    finally:
        context["finished_at"] = datetime.now(UTC).isoformat()
        context["duration_seconds"] = time.monotonic() - started
        write_context()
