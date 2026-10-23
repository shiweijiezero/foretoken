# SPDX-License-Identifier: Apache-2.0
# SPDX-FileCopyrightText: Copyright contributors to the Foretoken project

"""Read installed CNI configuration through the platform's node-inspection Pod."""

import json
import os
import sys
from pathlib import Path


def read_configuration(configuration: Path, binaries: list[Path], daemon: str) -> dict:
    """Return active CNI and shared IPAM documents after checking the installed plugin executables."""
    for name in ("macvlan", "whereabouts"):
        if not any(os.access(directory / name, os.X_OK) for directory in binaries):
            raise RuntimeError(
                f"CNI binary {name} is not installed in the runtime's binary directories"
            )
    files = [configuration / "whereabouts.d/whereabouts.conf"]
    for pattern in ("*.conf", "*.conflist", "*.json"):
        files.extend(path for path in configuration.glob(pattern) if path.is_file())
    if daemon:
        files.append(Path(daemon))
    return {str(path): json.loads(path.read_text()) for path in files}


if __name__ == "__main__":
    print(
        json.dumps(
            read_configuration(
                Path(sys.argv[1]),
                [Path(value) for value in json.loads(sys.argv[2])],
                sys.argv[3],
            )
        )
    )
