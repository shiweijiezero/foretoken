# SPDX-License-Identifier: Apache-2.0
# SPDX-FileCopyrightText: Copyright contributors to the Foretoken project

"""Apply the selected Foretoken patch series to an installed vLLM package."""

import importlib.metadata
import importlib.util
import py_compile
from pathlib import Path

from vllm_patches import apply_patch, engine_patches


def main() -> None:
    """Apply patches idempotently and compile each changed Python file once."""
    engine = importlib.metadata.distribution("vllm")
    module = importlib.util.find_spec("vllm")
    if module is None or module.origin is None:
        raise RuntimeError("Cannot locate the installed vLLM Python package")
    # GNU patch does not traverse package symlinks used by editable installations.
    package_root = Path(module.origin).resolve().parent.parent
    patches = engine_patches(package_root, engine.version, Path(__file__).resolve().parent)
    changed_files = {
        Path(line[6:].split()[0])
        for patch in patches
        for line in patch.read_text().splitlines()
        if line.startswith("+++ b/")
    }
    for patch_file in patches:
        apply_patch(package_root, patch_file)

    for path in sorted(changed_files):
        if path.suffix == ".py":
            py_compile.compile(str(package_root / path), doraise=True)


if __name__ == "__main__":
    main()
