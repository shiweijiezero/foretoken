# SPDX-License-Identifier: Apache-2.0
# SPDX-FileCopyrightText: Copyright contributors to the Foretoken project

"""Apply the Python engine backports shared by runtime images and source builds."""

from __future__ import annotations

import importlib.metadata
import py_compile
import subprocess
from pathlib import Path


def engine_patches(directory: Path, version: str, patches: Path) -> list[Path]:
    """Select the runtime's backports for image builds and editable engine workspaces."""
    ready = (
        "vllm-0.26-ready-logprobs.patch"
        if version.startswith("0.26.")
        else "vllm-ready-logprobs.patch"
    )
    names = ["vllm-python-profiling.patch", ready]
    if (
        version.startswith("0.30.")
        and (
            directory
            / "vllm/distributed/kv_transfer/kv_connector/v1/offloading/events.py"
        ).is_file()
    ):
        names.append("vllm-offloading-event-identity.patch")
    return [patches / name for name in names]


def apply_patch(directory: Path, patch: Path) -> None:
    """Apply one build-time backport, preserving checkouts where it is already installed."""
    arguments = [
        "patch",
        "--fuzz=0",
        "--strip=1",
        f"--directory={directory}",
        f"--input={patch}",
    ]
    applied = subprocess.run(
        arguments + ["--force", "--reverse", "--dry-run"],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        check=False,
    )
    if applied.returncode:
        subprocess.run(
            arguments + ["--batch", "--forward", "--no-backup-if-mismatch"], check=True
        )
    else:
        print(f"vLLM patch already installed: {patch.name}", flush=True)


def main() -> None:
    """Patch the installed engine and refresh its bytecode during runtime image construction."""
    distribution = importlib.metadata.distribution("vllm")
    directory = Path(distribution.locate_file(""))
    for patch in engine_patches(directory, distribution.version, Path(__file__).parent):
        apply_patch(directory, patch)
        for line in patch.read_text().splitlines():
            if line.startswith("+++ b/"):
                name = line.split()[1].removeprefix("b/")
                if name.endswith(".py"):
                    py_compile.compile(str(directory / name), doraise=True)


if __name__ == "__main__":
    main()
