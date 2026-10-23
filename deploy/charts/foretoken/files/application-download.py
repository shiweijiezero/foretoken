# SPDX-License-Identifier: Apache-2.0
# SPDX-FileCopyrightText: Copyright contributors to the Foretoken project

"""Install one published application directory before its Pod starts."""

import json
import logging
import os
import shutil
import sys
import tarfile
import time
from pathlib import Path, PurePosixPath
from urllib.parse import quote
from urllib.request import urlopen

logger = logging.getLogger(__name__)

# Init containers have no serving probe. Fail an idle transfer so kubelet can retry it.
_HTTP_IDLE_TIMEOUT = 30


def _relative_path(value: str) -> PurePosixPath:
    """Keep publication paths inside the application directory and its HTTP origin."""
    relative = PurePosixPath(value)
    if relative.is_absolute() or ".." in relative.parts or not relative.parts:
        raise ValueError(f"invalid application path: {value}")
    return relative


def _write_file(source, path: Path, entry: dict) -> None:
    """Install one manifest-listed regular file with its declared size and executable mode."""
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("wb") as output:
        shutil.copyfileobj(source, output)
    if path.stat().st_size != entry["size"]:
        raise OSError(f"incomplete application file: {entry['path']}")
    path.chmod(entry["mode"] & 0o777)


def install(reference: str, destination: Path) -> None:
    """Download a complete version into the Pod volume and expose it after success."""
    selected = destination / "current"
    receipt = selected / ".reference"
    if receipt.is_file() and receipt.read_text() == reference:
        return
    started = time.monotonic()
    destination.mkdir(parents=True, exist_ok=True)
    staging = destination / ".incoming"
    if staging.exists():
        shutil.rmtree(staging)
    staging.mkdir()
    origin = reference.rstrip("/")
    try:
        with urlopen(origin + "/manifest.json", timeout=_HTTP_IDLE_TIMEOUT) as response:
            manifest = json.load(response)
        entries = {_relative_path(entry["path"]): entry for entry in manifest["files"]}
        if "archive" in manifest:
            archive = _relative_path(manifest["archive"])
            logger.info("Downloading application archive")
            remaining = set(entries)
            with (
                urlopen(origin + "/" + quote(archive.as_posix(), safe="/"), timeout=_HTTP_IDLE_TIMEOUT) as response,
                tarfile.open(fileobj=response, mode="r|gz") as package,
            ):
                for member in package:
                    relative = _relative_path(member.name)
                    if not member.isfile() or relative not in remaining:
                        raise ValueError(f"unexpected application archive entry: {member.name}")
                    entry = entries[relative]
                    if member.size != entry["size"]:
                        raise OSError(f"incorrect application archive size: {relative}")
                    with package.extractfile(member) as source:
                        _write_file(source, staging.joinpath(*relative.parts), entry)
                    remaining.remove(relative)
            if remaining:
                raise OSError("application archive omitted files: " + ", ".join(map(str, sorted(remaining))))
        else:
            # Installed releases retain their immutable per-file publications.
            logger.info("Downloading application files")
            for relative, entry in entries.items():
                url = origin + "/" + quote(relative.as_posix(), safe="/")
                with urlopen(url, timeout=_HTTP_IDLE_TIMEOUT) as response:
                    _write_file(response, staging.joinpath(*relative.parts), entry)
        (staging / ".reference").write_text(reference)
        if selected.exists():
            shutil.rmtree(selected)
        os.rename(staging, selected)
        logger.info("Application files ready: %s files in %.1fs", len(entries), time.monotonic() - started)
    finally:
        if staging.exists():
            shutil.rmtree(staging)


def main() -> None:
    """Install the application's selected files for its init container."""
    logging.basicConfig(level=logging.INFO, format="%(message)s", stream=sys.stderr, force=True)
    install(sys.argv[1], Path(sys.argv[2]))


if __name__ == "__main__":
    main()
