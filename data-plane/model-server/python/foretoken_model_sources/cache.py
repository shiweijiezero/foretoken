# SPDX-License-Identifier: Apache-2.0
# SPDX-FileCopyrightText: Copyright contributors to the Foretoken project

"""Constrain provider collection to owned paths beneath the locked model cache."""

import shutil
import stat
from pathlib import Path


def cache_path(root: Path, *parts: str, file_link: bool = False) -> Path:
    """Keep every deletion beneath the managed root without traversing directory links."""
    target = root.joinpath(*parts)
    relative = target.relative_to(root)
    if not relative.parts or any(part in (".", "..") for part in relative.parts):
        raise ValueError(f"invalid cache path: {target}")
    current = root
    for part in relative.parts:
        current /= part
        try:
            mode = current.lstat().st_mode
        except FileNotFoundError:
            break
        if stat.S_ISLNK(mode):
            if current != target or not file_link:
                raise ValueError(f"cache path contains a directory symlink: {current}")
            resolved = current.resolve()
            if not resolved.is_relative_to(root) or resolved.is_dir():
                raise ValueError(f"cache link leaves its managed file area: {current}")
        elif current != target and not stat.S_ISDIR(mode):
            raise NotADirectoryError(str(current))
    return target


def remove(
    root: Path, target: Path, removed: set[str], *, file_link: bool = False
) -> None:
    """Remove one owned path, retaining the data root and all unrelated directories."""
    target = cache_path(root, *target.relative_to(root).parts, file_link=file_link)
    try:
        mode = target.lstat().st_mode
    except FileNotFoundError:
        return
    if stat.S_ISDIR(mode):
        shutil.rmtree(target)
    elif stat.S_ISREG(mode) or file_link and stat.S_ISLNK(mode):
        target.unlink()
    else:
        raise ValueError(f"unsupported cache entry: {target}")
    removed.add(target.relative_to(root).as_posix())
