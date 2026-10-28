# SPDX-License-Identifier: Apache-2.0
# SPDX-FileCopyrightText: Copyright contributors to the Foretoken project

"""Build the Kineto export binding against the selected engine's exact Torch ABI."""

import argparse
import os
import shlex
import subprocess
import sys
import sysconfig
from pathlib import Path

import torch


def main() -> None:
    """Compile the engine-environment artifact; inference needs no compiler or JIT build."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    torch_root = Path(torch.__file__).parent
    args.output.mkdir(parents=True, exist_ok=True)
    command = [*shlex.split(os.environ.get("CXX", "c++")), "-std=c++20", "-O2", "-shared", "-fPIC"]
    for include in (torch_root / "include", torch_root / "include/torch/csrc/api/include",
                    Path(sysconfig.get_path("include"))):
        command.extend(("-I", str(include)))
    command.append(f"-D_GLIBCXX_USE_CXX11_ABI={int(torch._C._GLIBCXX_USE_CXX11_ABI)}")
    if sys.platform == "darwin":
        command.extend(("-undefined", "dynamic_lookup"))
    library = torch_root / "lib"
    command.extend((str(Path(__file__).with_name("export.cpp")), "-L", str(library),
                    f"-Wl,-rpath,{library}", "-ltorch_python", "-ltorch_cpu", "-ltorch", "-lc10",
                    "-o", str(args.output / "foretoken_profile_native.so")))
    subprocess.run(command, check=True)


if __name__ == "__main__":
    main()
