# SPDX-License-Identifier: Apache-2.0
# SPDX-FileCopyrightText: Copyright contributors to the Foretoken project

"""Install one published application directory before its Pod starts."""

import json
import os
import shutil
import sys
from pathlib import Path, PurePosixPath
from urllib.parse import quote
from urllib.request import urlopen


def install(reference: str, destination: Path) -> None:
    """Download a complete version into the Pod volume and expose it after success."""
    selected = destination / "current"
    receipt = selected / ".reference"
    if receipt.is_file() and receipt.read_text() == reference:
        return
    destination.mkdir(parents=True, exist_ok=True)
    staging = destination / ".incoming"
    if staging.exists():
        shutil.rmtree(staging)
    staging.mkdir()
    try:
        with urlopen(reference.rstrip("/") + "/manifest.json") as response:
            manifest = json.load(response)
        for entry in manifest["files"]:
            relative = PurePosixPath(entry["path"])
            if relative.is_absolute() or ".." in relative.parts or not relative.parts:
                raise ValueError(f"invalid application path: {entry['path']}")
            path = staging.joinpath(*relative.parts)
            path.parent.mkdir(parents=True, exist_ok=True)
            url = reference.rstrip("/") + "/" + quote(relative.as_posix(), safe="/")
            with urlopen(url) as response, path.open("wb") as output:
                shutil.copyfileobj(response, output)
            if path.stat().st_size != entry["size"]:
                raise OSError(f"incomplete application file: {relative}")
            path.chmod(entry["mode"] & 0o777)
        (staging / ".reference").write_text(reference)
        if selected.exists():
            shutil.rmtree(selected)
        os.rename(staging, selected)
    finally:
        if staging.exists():
            shutil.rmtree(staging)


if __name__ == "__main__":
    install(sys.argv[1], Path(sys.argv[2]))
