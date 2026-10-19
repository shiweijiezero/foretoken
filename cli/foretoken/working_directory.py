# SPDX-License-Identifier: Apache-2.0
# SPDX-FileCopyrightText: Copyright contributors to the Foretoken project

"""Resolve shared repository data without relocating explicitly selected directories."""

from __future__ import annotations

import os
import subprocess
from pathlib import Path

from foretoken.errors import DeploymentError


def _checkout_roots(root: Path) -> tuple[Path, Path] | None:
    """Read the current and primary checkouts through Git's worktree metadata."""
    try:
        current = subprocess.run(
            ["git", "-C", str(root), "rev-parse", "--show-toplevel"],
            capture_output=True, text=True, check=False,
        )
        if current.returncode:
            return None
        worktrees = subprocess.run(
            ["git", "-C", str(root), "worktree", "list", "--porcelain", "-z"],
            capture_output=True, text=True, check=True,
        )
    except FileNotFoundError:
        return None
    except subprocess.CalledProcessError as error:
        raise DeploymentError(f"cannot read Git worktree paths: {error.stderr.strip()}") from error
    checkout = Path(current.stdout.strip()).resolve()
    primary = worktrees.stdout.split("\0\0", 1)[0].split("\0")
    if "bare" in primary:
        return checkout, checkout
    return checkout, Path(primary[0].removeprefix("worktree ")).resolve()


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
