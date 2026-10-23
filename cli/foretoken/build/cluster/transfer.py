# SPDX-License-Identifier: Apache-2.0
# SPDX-FileCopyrightText: Copyright contributors to the Foretoken project

"""Transfer captured source deltas into build workspaces without moving compiled outputs to the client."""

from __future__ import annotations

import io
import json
import os
import tarfile
import tempfile
import time
from collections.abc import Callable
from pathlib import Path

from loguru import logger

from foretoken.errors import DeploymentError
from foretoken.processes.execution import CommandExecution


def synchronize_inputs(
    command: Callable[[list[str]], list[str]],
    execution: CommandExecution,
    workspace: str,
    manifest: str,
    files: dict[str, Path],
    versions: dict[str, str],
) -> None:
    """Update a remote workspace from complete captured inputs, recovering interrupted transfers."""
    started = time.monotonic()
    previous_result = execution.run(
        command(["sh", "-ec", 'if test -f "$1"; then cat "$1"; else printf "{}"; fi', "read", manifest]),
        capture_output=True, text=True,
    )
    if previous_result.returncode:
        raise DeploymentError(previous_result.stderr.strip() or "could not read build workspace inputs")
    previous = json.loads(previous_result.stdout)
    changed = [name for name in files if previous.get(name) != versions[name]]
    removed = sorted(previous.keys() - versions.keys())
    if not changed and not removed:
        logger.info("Source transfer: reused unchanged workspace")
        return

    # Removing the receipt before publication makes a retry restore the full input
    # tree rather than trust files from a transfer that stopped between writes.
    script = '''set -eu
workspace=$1; receipt=$2; reset=$3
parent=$(dirname "$workspace")
mkdir -p "$parent"
incoming=$(mktemp -d "$parent/.incoming.XXXXXX")
trap 'rm -rf "$incoming"; rm -f "$incoming.inputs"' EXIT
tar --no-same-owner -xzf - -C "$incoming"
rm -f "$receipt"
mkdir -p "$workspace"
# A session client may bind-mount this directory; replace its contents, not its inode.
if test "$reset" = true; then find "$workspace" -mindepth 1 -maxdepth 1 -exec rm -rf -- {} +; fi
cd "$workspace"
xargs -0 -r rm -f -- < "$incoming/.foretoken-removed"
find . -mindepth 1 -depth -type d -empty -delete
rm "$incoming/.foretoken-removed"
mv "$incoming/.foretoken-inputs" "$incoming.inputs"
find "$incoming" -type f -exec touch {} +
cp -pRf "$incoming/." "$workspace/"
mkdir -p "$(dirname "$receipt")"
mv "$incoming.inputs" "$receipt"
'''
    with tempfile.TemporaryFile() as stream:
        with tarfile.open(fileobj=stream, mode="w:gz") as archive:
            for name in changed:
                archive.add(files[name], arcname=name, recursive=False)
            for name, data in (
                (".foretoken-removed", b"\0".join(os.fsencode(name) for name in removed)),
                (".foretoken-inputs", json.dumps(versions).encode()),
            ):
                item = tarfile.TarInfo(name)
                item.size = len(data)
                item.mode = 0o644
                archive.addfile(item, io.BytesIO(data))
        logger.info(
            "Source transfer: {} changed files, {} removed files, {} compressed bytes",
            len(changed), len(removed), stream.tell(),
        )
        stream.seek(0)
        result = execution.run(
            command(["sh", "-ec", script, "sync", workspace, manifest, str(not previous).lower()]),
            stdin=stream,
        )
    if result.returncode:
        raise DeploymentError("source delta upload failed")
    logger.info("Source transfer completed in {:.1f}s", time.monotonic() - started)
