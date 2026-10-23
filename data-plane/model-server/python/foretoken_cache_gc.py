# SPDX-License-Identifier: Apache-2.0
# SPDX-FileCopyrightText: Copyright contributors to the Foretoken project

"""Collect provider snapshots after their shared-cache consumers release them."""

import contextlib
import json
import os
import sys
from pathlib import Path

from foretoken_model_sources import huggingface, modelscope
from foretoken_model_sources.cache import cache_path, remove
from foretoken_model_sources.peer import distributed_directory


def collect_cache_items(root: Path, candidates: list[dict], live: list[dict]) -> dict:
    """Collect unreferenced artifacts for the caller holding the shared-cache lock.

    The caller owns consumer publication and retirement. Local or unsupported sources
    remain untouched; the result identifies collected, retained and removed paths.
    """
    root = Path(os.path.abspath(root))
    if root.is_symlink():
        raise ValueError(f"managed model cache root is a symlink: {root}")
    root = root.resolve()
    live_keys = {_item_key(item) for item in live}
    keys = sorted({_item_key(item) for item in candidates})
    result = {"collected": [], "retained": [], "removed_paths": []}
    removed = set()
    for source, repository, revision in keys:
        item = {"source": source, "repository": repository, "revision": revision}
        if (source, repository, revision) in live_keys or source not in (
            "hf",
            "modelscope",
        ):
            result["retained"].append(item)
            continue
        peer = distributed_directory(root, source, repository, revision)
        peer_paths = [
            peer,
            peer.with_name(f".{revision}.partial"),
            peer.with_name(f".{revision}.lock"),
        ]
        # Verify every peer path before either provider can delete its own native cache.
        for path in peer_paths:
            cache_path(root, *path.relative_to(root).parts)
        if source == "hf":
            live_revisions = {
                version
                for provider, repo, version in live_keys
                if provider == "hf" and repo == repository
            }
            huggingface.collect(root, repository, revision, live_revisions, removed)
        elif not modelscope.collect(root, repository, revision, live_keys, removed):
            result["retained"].append(item)
            continue
        for path in peer_paths:
            remove(root, path, removed)
        result["collected"].append(item)
    result["removed_paths"] = sorted(removed)
    return result


def main() -> None:
    """Read a locked consumer snapshot from stdin and emit one JSON collection result."""
    request = json.load(sys.stdin)
    with contextlib.redirect_stdout(sys.stderr):
        result = collect_cache_items(
            Path(request["root"]), request["candidates"], request["live"]
        )
    print(json.dumps(result))


def _item_key(item: dict) -> tuple[str, str, str]:
    """Validate explicit artifact identities before deriving provider deletion paths."""
    source, repository, revision = item["source"], item["repository"], item["revision"]
    if not all(
        isinstance(value, str) and value for value in (source, repository, revision)
    ):
        raise ValueError("cache items require nonempty source, repository and revision")
    parts = repository.split("/")
    if (
        any(part in ("", ".", "..") for part in parts)
        or len(parts) > 2
        or revision in (".", "..")
        or any(character in repository + revision for character in ("\\", "\x00"))
        or "/" in revision
    ):
        raise ValueError("cache item repository and revision must identify cache paths")
    if source == "modelscope" and len(parts) != 2:
        raise ValueError("ModelScope cache items require an owner/name repository")
    return source, repository, revision


if __name__ == "__main__":
    main()
