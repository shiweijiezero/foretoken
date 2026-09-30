# SPDX-License-Identifier: Apache-2.0
# SPDX-FileCopyrightText: Copyright contributors to the Foretoken project

"""Resolve source revisions and acquire immutable provider snapshots for model startup."""

import contextlib
import json
import os
import shutil
import subprocess
import sys
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path


def peer_snapshot(source, repository, revision, files, public, validate):
    """Materialize an anonymous public snapshot through the platform's revision-tagged peers."""
    socket = os.environ.get("FORETOKEN_DRAGONFLY_SOCKET")
    if not socket or not public:
        return None
    from filelock import FileLock

    directory = (
        Path(os.environ["FORETOKEN_MODEL_ROOT"])
        / "distributed"
        / source
        / repository
        / revision
    )
    directory.parent.mkdir(parents=True, exist_ok=True)
    # A version view is immutable after its atomic rename. Existing incomplete
    # views recover through the provider SDK rather than overwriting active readers.
    with FileLock(str(directory.with_name(f".{revision}.lock"))):
        if directory.is_dir():
            return (
                str(directory)
                if all((directory / name).is_file() for name in files)
                else None
            )
        temporary = directory.with_name(f".{revision}.partial")
        environment = dict(os.environ)
        environment["DFGET_HF_REVISION" if source == "hf" else "DFGET_MS_REVISION"] = (
            revision
        )

        def transfer(name):
            target = temporary / name
            target.parent.mkdir(parents=True, exist_ok=True)
            subprocess.run(
                [
                    "dfget",
                    f"{source}://{repository}/{name}",
                    "--output",
                    str(target),
                    "--endpoint",
                    socket,
                    "--tag",
                    revision,
                    "--transfer-from-dfdaemon",
                    "--overwrite",
                    "--no-progress",
                ],
                env=environment,
                stdout=sys.stderr,
                stderr=sys.stderr,
                check=True,
            )

        try:
            with ThreadPoolExecutor() as executor:
                list(executor.map(transfer, files))
        except subprocess.CalledProcessError as error:
            if temporary.exists():
                shutil.rmtree(temporary)
            print(
                f"Peer acquisition exited with status {error.returncode}; using the provider SDK",
                file=sys.stderr,
            )
            return None
        validate(temporary)
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
    """Keep native HF snapshots and blobs; optional peer views use the same resolved commit."""
    from huggingface_hub import (
        HfApi,
        get_token,
        snapshot_download,
        try_to_load_from_cache,
    )

    api = HfApi()
    if (
        os.environ.get("FORETOKEN_DRAGONFLY_SOCKET")
        and api.endpoint.rstrip("/") == "https://huggingface.co"
        and not get_token()
        and not os.environ.get("DFGET_HF_TOKEN")
    ):
        info = api.model_info(repository, revision=revision)
        files = [entry.rfilename for entry in info.siblings]
        cached = all(
            isinstance(try_to_load_from_cache(repository, name, revision=revision), str)
            for name in files
        )
        if not cached:

            def validate(directory):
                for name in files:
                    if not (directory / name).is_file():
                        raise FileNotFoundError(f"peer snapshot is missing {name}")

            peer = peer_snapshot(
                "hf",
                repository,
                revision,
                files,
                info.private is False and info.gated is False,
                validate,
            )
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
    """Use SDK cache and transfer primitives with an already resolved repository commit."""
    from modelscope.hub.api import HubApi, ModelScopeConfig
    from modelscope.hub.constants import DEFAULT_MAX_WORKERS, FILE_HASH, ModelVisibility
    from modelscope.hub.file_download import (
        create_temporary_directory_and_cache,
        download_file,
        file_integrity_validation,
        get_file_download_url,
    )
    from modelscope.hub.utils.utils import weak_file_lock

    api = HubApi()
    cookies = api.get_cookies()
    endpoint = api.get_endpoint_for_read(repo_id=repository, repo_type="model")
    version_cache = Path(os.environ["MODELSCOPE_CACHE"]) / "revisions" / revision
    locks = version_cache / ".lock"
    locks.mkdir(parents=True, exist_ok=True)
    # Match the SDK's repository lock and let it retain file-level resume,
    # parallel range requests, integrity validation and cache metadata updates.
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

        public = False
        if (
            os.environ.get("FORETOKEN_DRAGONFLY_SOCKET")
            and api.endpoint.rstrip("/") == "https://www.modelscope.cn"
            and not os.environ.get("MODELSCOPE_API_TOKEN")
            and not os.environ.get("DFGET_MS_TOKEN")
            and not ModelScopeConfig.get_token()
            and cookies is None
        ):
            info = api.get_model(repository)
            public = info["Visibility"] == ModelVisibility.PUBLIC and bool(
                info["IsAccessible"]
            )

        def validate(directory):
            for entry in entries:
                file = directory / entry["Path"]
                if not file.is_file():
                    raise FileNotFoundError(f"peer snapshot is missing {entry['Path']}")
                if entry.get(FILE_HASH):
                    file_integrity_validation(str(file), entry[FILE_HASH])

        peer = peer_snapshot(
            "modelscope", repository, revision, files, public, validate
        )
        if peer:
            return peer, files

        def download(entry):
            return download_file(
                get_file_download_url(repository, entry["Path"], revision, endpoint),
                entry,
                temporary,
                cache,
                api.builder_headers(api.headers),
                cookies,
            )

        with ThreadPoolExecutor(max_workers=DEFAULT_MAX_WORKERS) as executor:
            list(executor.map(download, pending))
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
