# SPDX-License-Identifier: Apache-2.0
# SPDX-FileCopyrightText: Copyright contributors to the Foretoken project

"""Resolve shared repository data without relocating explicitly selected directories."""

from __future__ import annotations

import codecs
import os
import subprocess
from pathlib import Path

from foretoken.errors import DeploymentError


def find_source_directory(root: Path) -> Path | None:
    """Find the nearest Foretoken build root for source discovery and default data placement."""
    root = root.resolve()
    return next((
        directory for directory in (root, *root.parents)
        if (directory / "Makefile").is_file() and (directory / "deploy/charts/foretoken/Chart.yaml").is_file()
    ), None)


def _checkout_roots(root: Path) -> tuple[Path, Path] | None:
    """Read worktree metadata only for the selected Foretoken source directory."""
    source = find_source_directory(root)
    if source is None or not (source / ".git").exists():
        return None
    try:
        current = subprocess.run(
            ["git", "-C", str(source), "rev-parse", "--show-toplevel"],
            capture_output=True, check=False,
        )
        if current.returncode:
            return None
        checkout = Path(os.fsdecode(current.stdout.removesuffix(b"\n"))).resolve()
        if checkout != source:
            return None
        worktrees = subprocess.run(
            ["git", "-c", "core.quotePath=true", "-C", str(source), "worktree", "list", "--porcelain"],
            capture_output=True, check=True,
        )
    except FileNotFoundError:
        return None
    except subprocess.CalledProcessError as error:
        raise DeploymentError(f"cannot read Git worktree paths: {os.fsdecode(error.stderr).strip()}") from error
    primary = worktrees.stdout.split(b"\n\n", 1)[0].splitlines()
    if b"bare" in primary:
        return checkout, checkout
    path = primary[0].removeprefix(b"worktree ")
    if path.startswith(b'"'):
        path = codecs.escape_decode(path[1:-1])[0]
    return checkout, Path(os.fsdecode(path)).resolve()


def repository_data_directory(root: Path) -> Path:
    """Resolve repository-default data for installation and local cluster creation."""
    source = find_source_directory(root) or root
    checkouts = _checkout_roots(source)
    return ((checkouts[1] if checkouts is not None else source) / "data").resolve()


def resolve_runtime_directory(directory: str, root: Path) -> Path:
    """Resolve deployment paths, sharing only the repository-default data across worktrees."""
    selected = Path(directory).expanduser()
    if selected.is_absolute():
        return selected.resolve()
    selected = Path(os.path.abspath(root / selected))
    checkouts = _checkout_roots(root)
    if checkouts is not None and selected == checkouts[0] / "data":
        selected = checkouts[1] / "data"
    return selected.resolve()
