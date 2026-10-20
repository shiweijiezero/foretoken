# SPDX-License-Identifier: Apache-2.0
# SPDX-FileCopyrightText: Copyright contributors to the Foretoken project

"""Resolve shared repository data without relocating explicitly selected directories."""

from __future__ import annotations

import codecs
import os
import subprocess
from pathlib import Path

from foretoken.errors import DeploymentError


def _checkout_roots(root: Path) -> tuple[Path, Path] | None:
    """Read the current and primary checkouts through Git's worktree metadata."""
    try:
        current = subprocess.run(
            ["git", "-C", str(root), "rev-parse", "--show-toplevel"],
            capture_output=True, check=False,
        )
        if current.returncode:
            return None
        worktrees = subprocess.run(
            ["git", "-c", "core.quotePath=true", "-C", str(root), "worktree", "list", "--porcelain"],
            capture_output=True, check=True,
        )
    except FileNotFoundError:
        return None
    except subprocess.CalledProcessError as error:
        raise DeploymentError(f"cannot read Git worktree paths: {os.fsdecode(error.stderr).strip()}") from error
    checkout = Path(os.fsdecode(current.stdout.removesuffix(b"\n"))).resolve()
    primary = worktrees.stdout.split(b"\n\n", 1)[0].splitlines()
    if b"bare" in primary:
        return checkout, checkout
    path = primary[0].removeprefix(b"worktree ")
    if path.startswith(b'"'):
        path = codecs.escape_decode(path[1:-1])[0]
    return checkout, Path(os.fsdecode(path)).resolve()


def repository_data_directory(root: Path) -> Path:
    """Return the primary checkout's data directory for cluster and namespace preparation."""
    checkouts = _checkout_roots(root)
    return ((checkouts[1] if checkouts is not None else root) / "data").resolve()


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
