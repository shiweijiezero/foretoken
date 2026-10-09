# SPDX-License-Identifier: Apache-2.0
# SPDX-FileCopyrightText: Copyright contributors to the Foretoken project

"""Apply the selected Foretoken patch series to an installed vLLM package."""

import importlib.metadata
import py_compile
from pathlib import Path

from vllm_patches import apply_patch, engine_patches


def main() -> None:
    """Apply patches idempotently and compile each changed Python file once."""
    engine = importlib.metadata.distribution("vllm")
    site = Path(engine.locate_file(""))
    patches = engine_patches(site, engine.version, Path(__file__).resolve().parent)
    changed_files = {
        Path(line[6:].split()[0])
        for patch in patches
        for line in patch.read_text().splitlines()
        if line.startswith("+++ b/")
    }
    for patch_file in patches:
        apply_patch(site, patch_file)

    for path in sorted(changed_files):
        if path.suffix == ".py":
            py_compile.compile(str(site / path), doraise=True)


if __name__ == "__main__":
    main()
