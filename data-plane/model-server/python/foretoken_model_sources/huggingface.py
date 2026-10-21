# SPDX-License-Identifier: Apache-2.0
# SPDX-FileCopyrightText: Copyright contributors to the Foretoken project

"""Resolve, acquire and collect Hugging Face snapshots while preserving native blob ownership."""

import hashlib
import logging
import os
import shutil
import stat
from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
from pathlib import Path

from .cache import cache_path, remove
from .peer import cached_peer_snapshot, distributed_directory, download_peer_file

logger = logging.getLogger(__name__)


def resolve(repository, revision):
    """Resolve the requested reference once before the preparation owner publishes it."""
    from huggingface_hub import HfApi

    return HfApi().model_info(repository, revision=revision).sha


def snapshot(repository, revision):
    """Keep native HF cache recovery and authenticate peer bytes against provider metadata."""
    import httpx
    import requests
    from huggingface_hub import (
        HfApi,
        get_token,
        hf_hub_url,
        snapshot_download,
        try_to_load_from_cache,
    )
    from huggingface_hub.constants import HF_HUB_ETAG_TIMEOUT
    from huggingface_hub.utils import HfHubHTTPError, OfflineModeIsEnabled

    root = Path(os.environ["FORETOKEN_MODEL_CACHE_ROOT"])
    api = HfApi()
    if (
        api.endpoint.rstrip("/") == "https://huggingface.co"
        and not get_token()
        and (
            os.environ.get("FORETOKEN_MODEL_DOWNLOAD_PROXY")
            or distributed_directory(root, "hf", repository, revision).is_dir()
        )
    ):
        try:
            info = api.model_info(
                repository,
                revision=revision,
                files_metadata=True,
                timeout=HF_HUB_ETAG_TIMEOUT,
            )
        except (
            HfHubHTTPError,
            OfflineModeIsEnabled,
            httpx.TransportError,
            requests.ConnectionError,
            requests.Timeout,
            requests.exceptions.ChunkedEncodingError,
        ):
            # The SDK owns offline and metadata-unavailable recovery from its native snapshot.
            info = None
        if info is not None:
            files = [entry.rfilename for entry in info.siblings]
            cached = {
                name: try_to_load_from_cache(repository, name, revision=revision)
                for name in files
            }
            if (
                not all(isinstance(path, str) for path in cached.values())
                and info.private is False
                and info.gated is False
            ):
                verifiable = all(
                    entry.size is not None
                    and (entry.lfs.sha256 if entry.lfs else entry.blob_id)
                    for entry in info.siblings
                )

                def validate(directory):
                    # Authenticate peer bytes against the provider's HTTPS metadata.
                    for entry in info.siblings:
                        file = directory / entry.rfilename
                        if not file.is_file() or file.stat().st_size != entry.size:
                            return False
                        if entry.lfs:
                            digest = hashlib.sha256()
                            expected = entry.lfs.sha256
                        else:
                            digest = hashlib.sha1(f"blob {entry.size}\0".encode())
                            expected = entry.blob_id
                        with file.open("rb") as content:
                            while data := content.read(shutil.COPY_BUFSIZE):
                                digest.update(data)
                        if digest.hexdigest() != expected:
                            return False
                    return True

                sources = (
                    {
                        name: Path(path)
                        if isinstance(path, str)
                        else hf_hub_url(repository, name, revision=revision)
                        for name, path in cached.items()
                    }
                    if verifiable
                    else None
                )
                peer = _peer_snapshot(
                    root, repository, revision, files, sources, validate
                )
                if peer:
                    return peer, files
    directory = Path(snapshot_download(repo_id=repository, revision=revision))
    files = [
        path.relative_to(directory).as_posix()
        for path in directory.rglob("*")
        if path.is_file()
    ]
    return str(directory), files


def collect(
    root: Path,
    repository: str,
    revision: str,
    live_revisions: set[str],
    removed: set[str],
) -> None:
    """Use Hub deletion planning while preserving every remaining cross-repository blob link."""
    hub = cache_path(root, "hub")
    repo_path = cache_path(root, "hub", "models--" + repository.replace("/", "--"))
    snapshot = cache_path(
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
        # A partial download has no revision-indexed snapshot; retain its blobs while any version lives.
        if live_revisions or repo is not None and repo.revisions:
            return
        snapshots = cache_path(root, *repo_path.relative_to(root).parts, "snapshots")
        if snapshots.exists() and any(snapshots.iterdir()):
            raise ValueError(
                f"Hub cannot safely index cache repository {repository}: {info.warnings}"
            )
        _check_repository(repo_path)
        for path, (_, payload) in links.items():
            if payload.is_relative_to(repo_path) and not path.is_relative_to(repo_path):
                raise ValueError(
                    f"Hub deletion tree still contains a referenced payload: {payload}"
                )
            if path.is_relative_to(repo_path) and not payload.is_relative_to(repo_path):
                raise ValueError(
                    f"Hub partial cache targets an unmanaged payload: {payload}"
                )
        remove(root, repo_path, removed)
        return

    # Revision hashes alone do not select a repository in the public Hub API.
    # Keep version-specific fields, including newer shared-blob ownership.
    strategy = replace(info, repos=frozenset({repo})).delete_revisions(revision)
    if strategy.repos:
        if live_revisions - {revision}:
            raise ValueError(
                f"Hub repository {repository} still has live revisions without cached snapshots"
            )
        _check_repository(repo_path)
    trees = set(strategy.repos) | set(strategy.snapshots)
    entries = set(getattr(strategy, "files", ()))

    def disappearing(path):
        return path in entries or any(path.is_relative_to(tree) for tree in trees)

    # Older scanners resolve local blob links; remove abandoned links without confusing them with payloads.
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
        marker = cache_path(root, "hub", "blobs", ".huggingface-shared-blobs")
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
        cache_path(root, *target.relative_to(root).parts, file_link=True)
    present = {
        path for path in targets | payloads if path.exists() or path.is_symlink()
    }
    strategy.execute()
    # Hub versions log some filesystem failures rather than raising them.
    for target in targets:
        if target.exists() or target.is_symlink():
            raise OSError(f"Hub cache deletion did not remove {target}")
    for target in abandoned_links:
        remove(root, target, removed, file_link=True)
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


def _peer_snapshot(root, repository, revision, files, sources, validate):
    """Publish verified peer files alongside links to existing native cache entries."""
    from filelock import FileLock

    directory = distributed_directory(root, "hf", repository, revision)
    proxy = os.environ.get("FORETOKEN_MODEL_DOWNLOAD_PROXY")
    if not directory.is_dir() and (not proxy or sources is None):
        return None
    directory.parent.mkdir(parents=True, exist_ok=True)
    # Published views remain valid independently of a live peer transport.
    with FileLock(str(directory.with_name(f".{revision}.lock"))):
        if directory.is_dir():
            return cached_peer_snapshot(root, "hf", repository, revision, files)
        if not proxy or sources is None:
            return None
        temporary = directory.with_name(f".{revision}.partial")
        # An interrupted view may link native blobs; never reopen those inodes for writing.
        if temporary.exists():
            shutil.rmtree(temporary)

        def transfer(name):
            target = temporary / name
            source = sources[name]
            if isinstance(source, Path):
                try:
                    target.parent.mkdir(parents=True, exist_ok=True)
                    target.unlink(missing_ok=True)
                    target.hardlink_to(source.resolve())
                except OSError as error:
                    return f"{name}: {error}"
                return None
            error = download_peer_file(proxy, source, revision, target)
            return f"{name}: {error}" if error is not None else None

        with ThreadPoolExecutor() as executor:
            errors = [error for error in executor.map(transfer, files) if error]
        if errors or not validate(temporary):
            if temporary.exists():
                shutil.rmtree(temporary)
            reason = (
                errors[0]
                if errors
                else "downloaded files did not match provider metadata"
            )
            logger.warning("Peer acquisition unavailable (%s); using the provider SDK", reason)
            return None
        temporary.rename(directory)
    return str(directory)


def _hub_links(hub: Path) -> dict[Path, tuple[Path, Path]]:
    """Read link facts without allowing the SDK scanner to traverse external storage."""
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


def _check_repository(repo_path: Path) -> None:
    """Limit whole-repository cleanup to the provider-owned cache layout."""
    directories = {"blobs", "refs", "snapshots", "trees", ".no_exist"}
    for entry in repo_path.iterdir():
        if entry.name not in directories or not stat.S_ISDIR(entry.lstat().st_mode):
            raise ValueError(f"Hub repository contains an unmanaged entry: {entry}")
