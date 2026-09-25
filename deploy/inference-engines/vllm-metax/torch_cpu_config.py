# SPDX-License-Identifier: Apache-2.0
# SPDX-FileCopyrightText: Copyright contributors to the Foretoken project

"""Export the installed MetaX Torch CPU build inputs for torchaudio's CMake build."""

from pathlib import Path
import sys

import torch
from torch.utils.cpp_extension import CppExtension


def write_torch_cpu_config(directory: Path) -> None:
    """Write a build-local Torch package consumed through torchaudio's Torch_ROOT.

    MetaX Torch's CMake package requires a CUDA toolkit even for CPU extensions.
    Its public extension API supplies the actual headers and libraries instead.
    """
    extension = CppExtension("torchaudio_cpu", sources=[])
    libraries = {}
    for name in extension.libraries:
        libraries[name] = next(
            str(Path(root) / f"lib{name}.so")
            for root in extension.library_dirs
            if (Path(root) / f"lib{name}.so").is_file()
        )
    abi = int(torch.compiled_with_cxx11_abi())
    directory.mkdir()
    (directory / "TorchConfig.cmake").write_text(
        f'''set(TORCH_INSTALL_PREFIX "{Path(torch.__file__).parent}")
set(TORCH_CXX_FLAGS "-DUSE_MACA -D_GLIBCXX_USE_CXX11_ABI={abi}")
add_library(torch_cpu INTERFACE)
set_target_properties(torch_cpu PROPERTIES INTERFACE_LINK_LIBRARIES "{libraries['torch_cpu']}")
add_library(torch INTERFACE)
set_target_properties(torch PROPERTIES
  INTERFACE_INCLUDE_DIRECTORIES "{';'.join(extension.include_dirs)}"
  INTERFACE_LINK_LIBRARIES "{';'.join(libraries.values())}")
set(Torch_FOUND TRUE)
'''
    )


if __name__ == "__main__":
    write_torch_cpu_config(Path(sys.argv[1]))
