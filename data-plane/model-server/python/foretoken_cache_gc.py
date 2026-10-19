# SPDX-License-Identifier: Apache-2.0
# SPDX-FileCopyrightText: Copyright contributors to the Foretoken project

"""Collect provider snapshots after their shared-cache consumers release them."""

import contextlib
import json
import os
import shutil
import stat
import sys
from dataclasses import replace
from pathlib import Path


def _item_key(item: dict) -> tuple[str, str, str]:
    """Validate the explicit artifact identity before deriving deletion paths."""
    source, repository, revision = (
        item["source"],
        item["repository"],
        item["revision"],
    )
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


def _cache_path(root: Path, *parts: str, file_link: bool = False) -> Path:
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


def _remove(
    root: Path, target: Path, removed: set[str], *, file_link: bool = False
) -> None:
    """Remove one owned path, retaining the data root and all unrelated directories."""
    target = _cache_path(root, *target.relative_to(root).parts, file_link=file_link)
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


def _hub_links(hub: Path) -> dict[Path, tuple[Path, Path]]:
    """Read Hub link facts without allowing the SDK scanner to traverse external storage."""
    links = {}

    def scan_error(error):
        raise error

    for directory, directories, files in os.walk(
        hub, followlinks=False, onerror=scan_error
    ):
        parent = Path(directory)
        for name in directories:
            path = parent / name
            if path.is_symlink():
                raise ValueError(f"Hub cache contains a directory symlink: {path}")
        for name in files:
            path = parent / name
            if not path.is_symlink():
                continue
            first = Path(os.path.abspath(path.parent / os.readlink(path)))
            resolved = path.resolve()
            if (
                not first.is_relative_to(hub)
                or not resolved.is_relative_to(hub)
                or resolved.is_dir()
            ):
                raise ValueError(f"Hub cache link leaves its managed file area: {path}")
            links[path] = first, resolved
    return links


def _check_hf_repository(repo_path: Path) -> None:
    """Limit whole-repository cleanup to the provider-owned cache layout."""
    directories = {"blobs", "refs", "snapshots", "trees", ".no_exist"}
    for entry in repo_path.iterdir():
        if entry.name not in directories or not stat.S_ISDIR(entry.lstat().st_mode):
            raise ValueError(f"Hub repository contains an unmanaged entry: {entry}")


def _collect_hf(
    root: Path,
    repository: str,
    revision: str,
    live_revisions: set[str],
    removed: set[str],
) -> None:
    """Use Hub deletion planning while preserving every remaining cross-repository blob link."""
    hub = _cache_path(root, "hub")
    repo_path = _cache_path(root, "hub", "models--" + repository.replace("/", "--"))
    snapshot = _cache_path(
        root, *repo_path.relative_to(root).parts, "snapshots", revision
    )
    if not repo_path.exists():
        return

    from huggingface_hub import scan_cache_dir

    links = _hub_links(hub)
    info = scan_cache_dir(hub)
    repo = next(
        (
            repo
            for repo in info.repos
            if repo.repo_type == "model" and repo.repo_id == repository
        ),
        None,
    )
    selected = (
        next((entry for entry in repo.revisions if entry.commit_hash == revision), None)
        if repo is not None
        else None
    )
    if selected is None:
        if snapshot.exists():
            raise ValueError(f"Hub cannot safely index cache snapshot: {snapshot}")
        # Interrupted downloads have no revision-indexed snapshot. Their blobs
        # belong to the repository, so collect them only after every version leaves.
        if live_revisions or repo is not None and repo.revisions:
            return
        snapshots = _cache_path(root, *repo_path.relative_to(root).parts, "snapshots")
        if snapshots.exists() and any(snapshots.iterdir()):
            raise ValueError(
                f"Hub cannot safely index cache repository {repository}: {info.warnings}"
            )
        _check_hf_repository(repo_path)
        for path, (_, payload) in links.items():
            if payload.is_relative_to(repo_path) and not path.is_relative_to(repo_path):
                raise ValueError(
                    f"Hub deletion tree still contains a referenced payload: {payload}"
                )
            if path.is_relative_to(repo_path) and not payload.is_relative_to(repo_path):
                raise ValueError(
                    f"Hub partial cache targets an unmanaged payload: {payload}"
                )
        _remove(root, repo_path, removed)
        return

    # Revision hashes alone do not select a repository in the public Hub API.
    # Keep its version-specific fields, including newer shared-blob ownership.
    strategy = replace(info, repos=frozenset({repo})).delete_revisions(revision)
    if strategy.repos:
        if live_revisions - {revision}:
            raise ValueError(
                f"Hub repository {repository} still has live revisions without cached snapshots"
            )
        _check_hf_repository(repo_path)
    trees = set(strategy.repos) | set(strategy.snapshots)
    entries = set(getattr(strategy, "files", ()))

    def disappearing(path):
        return path in entries or any(path.is_relative_to(tree) for tree in trees)

    # Older scanners resolve repo-local blob links to their physical targets.
    # Remove an abandoned local link too, but never confuse it with its payload.
    remaining_entries = {
        first for path, (first, _) in links.items() if not disappearing(path)
    }
    abandoned_links = {
        first
        for path, (first, _) in links.items()
        if disappearing(path)
        and first.parent == repo_path / "blobs"
        and first in links
        and first not in remaining_entries
    }
    protected = {
        resolved
        for path, (_, resolved) in links.items()
        if not disappearing(path) and path not in abandoned_links
    }
    for payload in protected:
        if any(payload.is_relative_to(tree) for tree in trees):
            raise ValueError(
                f"Hub deletion tree still contains a referenced payload: {payload}"
            )
    payloads = {entry.blob_path for entry in selected.files} | {
        resolved for path, (_, resolved) in links.items() if disappearing(path)
    }
    supports_shared_store = getattr(strategy, "cache_dir", None) == hub
    for payload in payloads - protected:
        if payload.is_relative_to(repo_path / "blobs") or payload.is_relative_to(
            selected.snapshot_path
        ):
            continue
        if not supports_shared_store:
            raise ValueError(
                "installed huggingface_hub cannot safely collect a cache-wide shared blob: "
                f"{payload}"
            )
        store = hub / "blobs"
        marker = _cache_path(root, "hub", "blobs", ".huggingface-shared-blobs")
        if not payload.is_relative_to(store):
            raise ValueError(f"Hub snapshot targets an unmanaged payload: {payload}")
        parts = payload.relative_to(store).parts
        if (
            len(parts) != 2
            or len(parts[0]) != 2
            or len(parts[1]) != 64
            or any(character not in "0123456789abcdef" for character in parts[1])
            or not parts[1].startswith(parts[0])
            or not marker.is_file()
            or marker.read_text() != "1\n"
        ):
            raise ValueError(f"Hub shared blob layout is not managed: {payload}")
    strategy = replace(
        strategy,
        blobs=frozenset(path for path in strategy.blobs if path not in protected),
    )
    targets = (
        set(strategy.repos)
        | set(strategy.snapshots)
        | set(strategy.refs)
        | set(strategy.blobs)
        | entries
    )
    for target in targets:
        _cache_path(root, *target.relative_to(root).parts, file_link=True)
    present = {
        path for path in targets | payloads if path.exists() or path.is_symlink()
    }
    strategy.execute()
    # Hub versions log some filesystem failures rather than raising them.
    for target in targets:
        if target.exists() or target.is_symlink():
            raise OSError(f"Hub cache deletion did not remove {target}")
    for target in abandoned_links:
        _remove(root, target, removed, file_link=True)
    for payload in payloads - protected:
        if payload.exists() or payload.is_symlink():
            raise OSError(
                f"Hub could not safely collect the unreferenced payload: {payload}"
            )
    removed.update(
        path.relative_to(root).as_posix()
        for path in present
        if not path.exists() and not path.is_symlink()
    )


def collect_cache_items(root: Path, candidates: list[dict], live: list[dict]) -> dict:
    """Collect unreferenced remote artifacts for the caller holding the shared-cache lock.

    The caller owns consumer publication and retirement. Local or unsupported sources
    remain untouched; the result identifies collected, retained and removed paths.
    """
    root = Path(os.path.abspath(root))
    if root.is_symlink():
        raise ValueError(f"managed model cache root is a symlink: {root}")
    root = root.resolve()
    live_keys = {_item_key(item) for item in live}
    modelscope_paths = {
        (
            repository.split("/")[0],
            repository.split("/")[1].replace(".", "___"),
            revision,
        )
        for source, repository, revision in live_keys
        if source == "modelscope"
    }
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
        peer = _cache_path(
            root, "distributed", source, *repository.split("/"), revision
        )
        peer_paths = [
            peer,
            peer.with_name(f".{revision}.partial"),
            peer.with_name(f".{revision}.lock"),
        ]
        for path in peer_paths:
            _cache_path(root, *path.relative_to(root).parts)
        if source == "hf":
            live_revisions = {
                version
                for provider, repo, version in live_keys
                if provider == "hf" and repo == repository
            }
            _collect_hf(root, repository, revision, live_revisions, removed)
        else:
            owner, name = repository.split("/")
            if (owner, name.replace(".", "___"), revision) in modelscope_paths:
                result["retained"].append(item)
                continue
            # SDK cache constructors load pickle metadata from the shared volume.
            # Derive only the layout used by preparation; never deserialize it for GC.
            version = _cache_path(root, "modelscope", "revisions", revision)
            paths = [
                version / owner / name.replace(".", "___"),
                version / "._____temp" / owner / name,
                version / ".lock" / repository.replace("/", "___"),
            ]
            for path in paths:
                _cache_path(root, *path.relative_to(root).parts)
            for path in paths:
                _remove(root, path, removed)
        for path in peer_paths:
            _remove(root, path, removed)
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


if __name__ == "__main__":
    main()
