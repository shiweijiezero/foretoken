# SPDX-License-Identifier: Apache-2.0
# SPDX-FileCopyrightText: Copyright contributors to the Foretoken project

"""Resolve, acquire and collect fixed-revision ModelScope snapshots using their native cache layout."""

import logging
import os
import shutil
from pathlib import Path

from . import acquire_in_parallel
from .cache import cache_path, remove
from .peer import cached_peer_snapshot, distributed_directory, download_peer_file

logger = logging.getLogger(__name__)


def resolve(repository, revision):
    """Resolve the requested reference once before the preparation owner publishes it."""
    from modelscope.hub.api import HubApi

    history = HubApi().list_repo_commits(repository, revision=revision, page_size=1)
    return history.commits[0].id


def snapshot(repository, revision):
    """Acquire fixed-revision files while the SDK owns locking, resume and cache publication."""
    import requests
    from modelscope.hub.api import HubApi, ModelScopeConfig
    from modelscope.hub.constants import DEFAULT_MAX_WORKERS, FILE_HASH, ModelVisibility
    from modelscope.hub.errors import NotExistError
    from modelscope.hub.file_download import (
        create_temporary_directory_and_cache,
        download_file,
        get_file_download_url,
    )
    from modelscope.hub.utils.utils import file_integrity_validation, weak_file_lock

    root = Path(os.environ["FORETOKEN_MODEL_CACHE_ROOT"])
    api = HubApi()
    cookies = api.get_cookies()
    endpoint = api.get_endpoint_for_read(repo_id=repository, repo_type="model")
    version_cache = Path(os.environ["MODELSCOPE_CACHE"]) / "revisions" / revision
    locks = version_cache / ".lock"
    locks.mkdir(parents=True, exist_ok=True)
    # Keep the SDK's lock, cache metadata, resume support and native file validation together.
    with weak_file_lock(locks / repository.replace("/", "___")):
        temporary, cache = create_temporary_directory_and_cache(
            repository, cache_dir=str(version_cache)
        )
        entries = [
            entry
            for entry in api.get_model_files(
                repository,
                revision=revision,
                recursive=True,
                use_cookies=False if cookies is None else cookies,
                endpoint=endpoint,
            )
            if entry["Type"] == "blob"
        ]
        files = [entry["Path"] for entry in entries]
        pending = [entry for entry in entries if not cache.exists(entry)]
        if not pending:
            return cache.get_root_location(), files

        proxy = os.environ.get("FORETOKEN_MODEL_DOWNLOAD_PROXY")
        public = False
        if (
            api.endpoint.rstrip("/") == "https://www.modelscope.cn"
            and not os.environ.get("MODELSCOPE_API_TOKEN")
            and not ModelScopeConfig.get_token()
            and cookies is None
            and (
                proxy
                or distributed_directory(
                    root, "modelscope", repository, revision
                ).is_dir()
            )
        ):
            try:
                info = api.get_model(repository, revision=revision)
            except (
                requests.ConnectionError,
                requests.Timeout,
                requests.HTTPError,
                requests.exceptions.RetryError,
                requests.exceptions.ChunkedEncodingError,
                NotExistError,
            ):
                # Unknown visibility excludes sharing, not the provider's own download path.
                pass
            else:
                public = info["Visibility"] == ModelVisibility.PUBLIC and bool(
                    info["IsAccessible"]
                )
        if public:
            previous = cached_peer_snapshot(
                root, "modelscope", repository, revision, files
            )
            if previous:
                return previous, files

        # The repository lock also owns recovery of interrupted peer transfers.
        staging = distributed_directory(
            root, "modelscope", repository, revision
        ).with_name(f".{revision}.partial")
        if staging.exists():
            shutil.rmtree(staging)
        resume = any(path.is_file() for path in Path(temporary).rglob("*"))

        def download(entry):
            url = get_file_download_url(repository, entry["Path"], revision, endpoint)
            # Unverified peer staging must never become the SDK's continuation prefix.
            if (
                public
                and proxy
                and entry.get(FILE_HASH)
                and entry.get("Size") is not None
                and not resume
            ):
                target = staging / entry["Path"]
                error = download_peer_file(proxy, url, revision, target)
                if error is None:
                    if target.stat().st_size == entry[
                        "Size"
                    ] and file_integrity_validation(str(target), entry[FILE_HASH]):
                        return cache.put_file(entry, str(target))
                    error = "downloaded file did not match provider metadata"
                target.unlink(missing_ok=True)
                logger.warning(
                    "Peer acquisition unavailable (%s: %s); using the provider SDK",
                    entry["Path"], error,
                )
            return download_file(
                url, entry, temporary, cache, api.builder_headers(api.headers), cookies
            )

        try:
            acquire_in_parallel(download, pending, max_workers=DEFAULT_MAX_WORKERS)
        finally:
            if staging.exists():
                shutil.rmtree(staging)
        return cache.get_root_location(), files


def collect(
    root: Path,
    repository: str,
    revision: str,
    live: set[tuple[str, str, str]],
    removed: set[str],
) -> bool:
    """Collect the prepared layout without loading pickle metadata; retain normalized live aliases."""
    owner, name = repository.split("/")
    aliases = {
        (repo.split("/")[0], repo.split("/")[1].replace(".", "___"), version)
        for source, repo, version in live
        if source == "modelscope"
    }
    if (owner, name.replace(".", "___"), revision) in aliases:
        return False
    # Preparation owns this revision-root layout; constructors would deserialize shared metadata.
    version = cache_path(root, "modelscope", "revisions", revision)
    paths = [
        version / owner / name.replace(".", "___"),
        version / "._____temp" / owner / name,
        version / ".lock" / repository.replace("/", "___"),
    ]
    for path in paths:
        cache_path(root, *path.relative_to(root).parts)
    for path in paths:
        remove(root, path, removed)
    return True
