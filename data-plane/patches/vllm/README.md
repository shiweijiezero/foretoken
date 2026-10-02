<!--
SPDX-License-Identifier: Apache-2.0
SPDX-FileCopyrightText: Copyright contributors to the Foretoken project
-->

# Maintaining vLLM patches

English | [简体中文](README_zh.md)

| Location | Purpose |
| --- | --- |
| `common/` | Shared Python, communication, and native-code patches |
| `compatibility/` | Patch series and insertion points for different upstream interfaces |
| `rust/` | Patches to the upstream Rust crates |
| `source.series` | Ordered patches for the pinned source checkout, consumed by `make vllm-source` |
| `version-map.yaml` | Installed Python package versions mapped to a compatibility series |

Series entries are relative to this directory. Patches use `-p1` against the upstream repository root or the installed package's parent directory, both of which contain `vllm/`.

Edit the corresponding upstream source and regenerate unified diffs. Keep shared behavior in `common/`; compatibility patches retain only the differing imports, interfaces, and insertion points. Several package versions can use the same series. Extend the version mapping after checking the patch stack against those sources and exercising the affected runtime.

The model-server image runs `apply.py` to select a series, apply missing patches, and compile the changed Python files. Rust source preparation reads `source.series` without selecting a Python package version. Run source preparation or image construction again to check that an already-patched tree is accepted.

Patches for another library belong in a separate directory alongside `vllm/`.
