# SPDX-License-Identifier: Apache-2.0
# SPDX-FileCopyrightText: Copyright contributors to the Foretoken project

"""Stage peer transfers independently of provider caches and authenticated file verification."""

import contextlib
import errno
import http.client
import shutil
from pathlib import Path
from urllib.parse import urlsplit


def distributed_directory(
    root: Path, source: str, repository: str, revision: str
) -> Path:
    """Locate one peer snapshot for acquisition and collection, independent of its transport."""
    return root / "distributed" / source / repository / revision


def cached_peer_snapshot(root, source, repository, revision, files):
    """Return a published peer view when the provider's complete file list is present."""
    directory = distributed_directory(root, source, repository, revision)
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
