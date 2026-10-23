# SPDX-License-Identifier: Apache-2.0
# SPDX-FileCopyrightText: Copyright contributors to the Foretoken project

"""Identify inference-engine files supplied by native builds rather than editable source."""

from __future__ import annotations

from pathlib import PurePath


def generated_engine_file(path: PurePath) -> bool:
    """Keep vLLM's installed build outputs out of source capture and available to runtime overlays."""
    if path.name.endswith(".so") or ".so." in path.name:
        return True
    if len(path.parts) < 2 or path.parts[0] not in {"vllm", "vllm_metax"}:
        return False
    relative = path.relative_to(path.parts[0]).as_posix()
    if relative in {"_version.py", "vllm-rs"}:
        return True
    if path.parts[0] != "vllm":
        return False
    # These are installed by upstream CMake and selected by vLLM's precompiled
    # wheel extractor. Handwritten vendor modules remain ordinary source inputs.
    if relative == "third_party/flashmla/flash_mla_interface.py":
        return True
    if relative.startswith(("third_party/deep_gemm/", "third_party/fmha_sm100/")):
        return True
    if path.suffix != ".py":
        return False
    if relative.startswith(("third_party/triton_kernels/", "third_party/tml_fa4/")):
        return True
    return relative.startswith("vllm_flash_attn/") and relative not in {
        "vllm_flash_attn/__init__.py", "vllm_flash_attn/flash_attn_interface.py",
    }
