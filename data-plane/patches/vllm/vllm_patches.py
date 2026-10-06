# SPDX-License-Identifier: Apache-2.0
# SPDX-FileCopyrightText: Copyright contributors to the Foretoken project

"""Apply the shared vLLM compatibility series in image and source builds."""

from __future__ import annotations

import importlib.metadata
import py_compile
import subprocess
from pathlib import Path

import yaml
from packaging.specifiers import SpecifierSet
from packaging.version import Version


def engine_patches(directory: Path, version: str, patches: Path) -> list[Path]:
    """Select one source-compatible patch series for an installed or source vLLM tree."""
    version_value = Version(version)
    version_map = yaml.safe_load((patches / "version-map.yaml").read_text())
    series_name = version_map["default_series"]
    for profile in version_map["profiles"]:
        if SpecifierSet(profile["versions"]).contains(version_value, prereleases=True):
            series_name = profile["series"]
            break

    legacy_ready_backport = False
    ready_source = directory / "vllm/v1/engine/__init__.py"
    if series_name == "compatibility/v1-legacy/series" and ready_source.is_file():
        source = ready_source.read_text()
        legacy_ready_backport = (
            "tensor_parallel_size: int" in source
            and "decode_context_parallel_size: int" in source
        )
        if legacy_ready_backport:
            series_name = "compatibility/v1-default/series"

    selected = []
    for line in (patches / series_name).read_text().splitlines():
        name = line.split("#", 1)[0].strip()
        if not name:
            continue
        if legacy_ready_backport and name == "common/ready-logprobs.patch":
            name = "compatibility/v1-modern-ready-logprobs.patch"
        optional = version_map["optional_patches"].get(name)
        if optional is not None and (
            not SpecifierSet(optional["versions"]).contains(
                version_value, prereleases=True
            )
            or not (directory / optional["requires"]).is_file()
        ):
            continue
        selected.append(patches / name)

    profiler_result = (
        "vllm-0.26-profiler-result.patch"
        if version_value < Version("0.30.0.dev0")
        else "vllm-0.30-profiler-result.patch"
    )
    selected.append(patches / profiler_result)
    return selected


def _patch_content_present(directory: Path, patch: Path) -> bool:
    """Recognize applied hunks whose trailing context changed in a later patch."""
    target = None
    hunks: list[tuple[Path, list[str]]] = []
    last_additions: list[int] = []
    for line in patch.read_text().splitlines():
        if line.startswith("+++ b/"):
            target = Path(line.removeprefix("+++ b/"))
        elif line.startswith("@@ ") and target is not None:
            hunks.append((target, []))
            last_additions.append(0)
        elif hunks and line.startswith((" ", "+")):
            hunks[-1][1].append(line[1:])
            if line.startswith("+"):
                last_additions[-1] = len(hunks[-1][1])
    try:
        # Keep each insertion with its preceding context, not isolated lines that
        # may already occur elsewhere. Later patches may replace trailing context.
        return bool(hunks) and all(
            end > 0
            and "\n" + "\n".join(lines[:end]) + "\n"
            in "\n" + (directory / path).read_text() + "\n"
            for (path, lines), end in zip(hunks, last_additions, strict=True)
        )
    except OSError:
        return False


def apply_patch(directory: Path, patch: Path) -> None:
    """Apply one patch while recognizing a completed patch with changed context."""
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
        if _patch_content_present(directory, patch):
            print(f"vLLM patch already installed: {patch.name}", flush=True)
            return
        subprocess.run(
            arguments + ["--batch", "--forward", "--no-backup-if-mismatch"], check=True
        )
    else:
        print(f"vLLM patch already installed: {patch.name}", flush=True)


def main() -> None:
    """Apply the selected installed-engine series and compile changed Python files."""
    distribution = importlib.metadata.distribution("vllm")
    directory = Path(distribution.locate_file(""))
    patches = engine_patches(directory, distribution.version, Path(__file__).parent)
    changed_files = {
        Path(line[6:].split()[0])
        for patch in patches
        for line in patch.read_text().splitlines()
        if line.startswith("+++ b/")
    }
    for patch in patches:
        apply_patch(directory, patch)
    for path in sorted(changed_files):
        if path.suffix == ".py":
            py_compile.compile(str(directory / path), doraise=True)


if __name__ == "__main__":
    main()
