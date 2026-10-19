# SPDX-License-Identifier: Apache-2.0
# SPDX-FileCopyrightText: Copyright contributors to the Foretoken project

"""Resolve source revisions and acquire immutable provider snapshots for model startup."""

import contextlib
import errno
import hashlib
import http.client
import json
import os
import shutil
import sys
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from urllib.parse import urlsplit


def distributed_directory(source, repository, revision):
    """Locate an immutable peer snapshot independently of the current transport endpoint."""
    return (
        Path(os.environ["FORETOKEN_MODEL_ROOT"])
        / "distributed"
        / source
        / repository
        / revision
    )


def cached_peer_snapshot(source, repository, revision, files):
    """Return a published peer view when the provider's complete file list is present."""
    directory = distributed_directory(source, repository, revision)
    if directory.is_dir() and all((directory / name).is_file() for name in files):
        return str(directory)
    return None


def download_peer_file(proxy, url, revision, target):
    """Acquire one file for provider verification; return an optional transport failure."""
    from huggingface_hub.constants import HF_HUB_DOWNLOAD_TIMEOUT

    endpoint = urlsplit(proxy)
    # Absolute-form HTTPS retains native back-to-source HTTPS without a CONNECT tunnel.
    with contextlib.closing(
        http.client.HTTPConnection(
            endpoint.hostname, endpoint.port, timeout=HF_HUB_DOWNLOAD_TIMEOUT
        )
    ) as connection:
        try:
            connection.request(
                "GET",
                url,
                headers={"X-Dragonfly-Use-P2P": "true", "X-Dragonfly-Tag": revision},
            )
            response = connection.getresponse()
        except (OSError, http.client.HTTPException) as error:
            return str(error)
        with response:
            if response.status != 200:
                return f"HTTP {response.status} {response.reason}"
            try:
                target.parent.mkdir(parents=True, exist_ok=True)
                with target.open("wb") as output:
                    while True:
                        try:
                            data = response.read(shutil.COPY_BUFSIZE)
                        except (OSError, http.client.HTTPException) as error:
                            return str(error)
                        if not data:
                            break
                        output.write(data)
            except OSError as error:
                # Releasing peer staging lets the SDK finish a partially cached download.
                if error.errno not in {errno.ENOSPC, errno.EDQUOT}:
                    raise
                return str(error)
    return None


def hf_peer_snapshot(repository, revision, files, sources, validate):
    """Publish verified HF peer files alongside links to existing native cache entries."""
    from filelock import FileLock

    directory = distributed_directory("hf", repository, revision)
    proxy = os.environ.get("FORETOKEN_MODEL_DOWNLOAD_PROXY")
    if not directory.is_dir() and (not proxy or sources is None):
        return None
    directory.parent.mkdir(parents=True, exist_ok=True)
    # Published views are immutable. Their validity does not depend on a live peer service.
    with FileLock(str(directory.with_name(f".{revision}.lock"))):
        if directory.is_dir():
            return cached_peer_snapshot("hf", repository, revision, files)
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
                # Partial cache hits must not require another copy of existing model files.
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
            print(
                f"Peer acquisition unavailable ({reason}); using the provider SDK",
                file=sys.stderr,
            )
            return None
        temporary.rename(directory)
    return str(directory)


def resolve(source, repository, revision):
    """Resolve a moving reference before any file is downloaded or published."""
    if source == "hf":
        from huggingface_hub import HfApi

        return HfApi().model_info(repository, revision=revision).sha
    if source == "modelscope":
        from modelscope.hub.api import HubApi

        history = HubApi().list_repo_commits(repository, revision=revision, page_size=1)
        return history.commits[0].id
    raise ValueError(f"unsupported preparation source: {source}")


def hf_snapshot(repository, revision):
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
    from huggingface_hub.errors import HfHubHTTPError, OfflineModeIsEnabled

    api = HfApi()
    if (
        api.endpoint.rstrip("/") == "https://huggingface.co"
        and not get_token()
        and (
            os.environ.get("FORETOKEN_MODEL_DOWNLOAD_PROXY")
            or distributed_directory("hf", repository, revision).is_dir()
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
                    # Verify peer bytes against the provider's authenticated HTTPS metadata.
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
                peer = hf_peer_snapshot(repository, revision, files, sources, validate)
                if peer:
                    return peer, files
    snapshot = Path(snapshot_download(repo_id=repository, revision=revision))
    files = [
        path.relative_to(snapshot).as_posix()
        for path in snapshot.rglob("*")
        if path.is_file()
    ]
    return str(snapshot), files


def modelscope_snapshot(repository, revision):
    """Acquire each fixed-revision file through the pinned SDK's cache and publication owner."""
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
                or distributed_directory("modelscope", repository, revision).is_dir()
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
            previous = cached_peer_snapshot("modelscope", repository, revision, files)
            if previous:
                return previous, files

        # The repository lock also owns recovery of interrupted peer transfers.
        staging = distributed_directory("modelscope", repository, revision).with_name(
            f".{revision}.partial"
        )
        if staging.exists():
            shutil.rmtree(staging)
        # Serial partials and parallel pieces both remain with the SDK's resume owner.
        resume = any(path.is_file() for path in Path(temporary).rglob("*"))

        def download(entry):
            url = get_file_download_url(repository, entry["Path"], revision, endpoint)
            # Preserve native resume files. Peer staging is separate so unverified bytes
            # can never become an SDK continuation prefix.
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
                print(
                    f"Peer acquisition unavailable ({entry['Path']}: {error}); using the provider SDK",
                    file=sys.stderr,
                )
            return download_file(
                url,
                entry,
                temporary,
                cache,
                api.builder_headers(api.headers),
                cookies,
            )

        try:
            with ThreadPoolExecutor(max_workers=DEFAULT_MAX_WORKERS) as executor:
                list(executor.map(download, pending))
        finally:
            if staging.exists():
                shutil.rmtree(staging)
        return cache.get_root_location(), files


def main() -> None:
    """Emit one structured resolution or acquisition result; SDK output stays on stderr."""
    source = os.environ["FORETOKEN_PREPARE_SOURCE"]
    model = (
        os.environ["FORETOKEN_PREPARE_MODEL"],
        os.environ["FORETOKEN_PREPARE_MODEL_REVISION"],
    )
    tokenizer = (
        os.environ["FORETOKEN_PREPARE_TOKENIZER"],
        os.environ["FORETOKEN_PREPARE_TOKENIZER_REVISION"],
    )
    artifacts = [model] if tokenizer == model else [model, tokenizer]
    with contextlib.redirect_stdout(sys.stderr):
        if os.environ["FORETOKEN_PREPARE_ACTION"] == "resolve":
            with ThreadPoolExecutor(max_workers=len(artifacts)) as executor:
                revisions = list(
                    executor.map(lambda artifact: resolve(source, *artifact), artifacts)
                )
            result = {
                "model_revision": revisions[0],
                "tokenizer_revision": revisions[-1],
            }
        else:
            download = hf_snapshot if source == "hf" else modelscope_snapshot
            with ThreadPoolExecutor(max_workers=len(artifacts)) as executor:
                snapshots = list(
                    executor.map(lambda artifact: download(*artifact), artifacts)
                )
            result = {
                "model": snapshots[0][0],
                "tokenizer": snapshots[-1][0],
                "model_files": snapshots[0][1],
                "tokenizer_files": snapshots[-1][1],
            }
    print(json.dumps(result))


if __name__ == "__main__":
    main()
