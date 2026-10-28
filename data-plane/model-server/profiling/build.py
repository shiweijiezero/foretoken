# SPDX-License-Identifier: Apache-2.0
# SPDX-FileCopyrightText: Copyright contributors to the Foretoken project

"""Build the Kineto export binding against the selected engine's exact Torch ABI."""

import argparse
import os
import shlex
import shutil
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
    compiler = shlex.split(os.environ.get("CXX", "c++"))
    python_include = Path(sysconfig.get_path("include"))
    # CPU and MetaX skip Mooncake compilation, so this stage owns its build dependencies.
    packages = []
    if shutil.which(compiler[0]) is None:
        packages.append("build-essential")
    if not (python_include / "Python.h").is_file():
        packages.append(f"python{sys.version_info.major}.{sys.version_info.minor}-dev")
    if packages:
        environment = {**os.environ, "DEBIAN_FRONTEND": "noninteractive"}
        subprocess.run(["apt-get", "update"], env=environment, check=True)
        subprocess.run(["apt-get", "install", "-y", "--no-install-recommends", *packages],
                       env=environment, check=True)
    command = [*compiler, "-std=c++20", "-O2", "-shared", "-fPIC"]
    for include in (torch_root / "include", torch_root / "include/torch/csrc/api/include",
                    python_include):
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
