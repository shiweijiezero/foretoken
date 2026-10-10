# SPDX-License-Identifier: Apache-2.0
# SPDX-FileCopyrightText: Copyright contributors to the Foretoken project

"""Apply vLLM runtime compatibility in image and source builds."""

from __future__ import annotations

import importlib.metadata
import json
import os
import py_compile
import subprocess
import sys
import tempfile
from pathlib import Path

import yaml
from packaging.requirements import Requirement
from packaging.specifiers import SpecifierSet
from packaging.utils import canonicalize_name
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


def repair_installed_dependencies() -> None:
    """Repair unused OTel remnants before vLLM 0.31 image dependency installation."""
    if Version(importlib.metadata.version("vllm")).release[:2] != (0, 31):
        return
    installed = {
        canonicalize_name(distribution.metadata["Name"]): distribution
        for distribution in importlib.metadata.distributions()
    }
    common = installed.get("opentelemetry-exporter-otlp-common")
    if common is None or common.version != "0.66b0":
        return
    sdk = installed.get("opentelemetry-sdk")
    if sdk is None:
        return
    sdk_version = Version(sdk.version)
    if not any(
        canonicalize_name(requirement.name) == "opentelemetry-sdk"
        and (requirement.marker is None or requirement.marker.evaluate())
        and sdk_version not in requirement.specifier
        for requirement in map(Requirement, common.requires or [])
    ):
        return
    remnants = {"opentelemetry-exporter-otlp-common"}
    transport = installed.get("opentelemetry-exporter-http-transport")
    if transport is not None and transport.version == common.version:
        remnants.add("opentelemetry-exporter-http-transport")
    retained = [dist for name, dist in installed.items() if name not in remnants]
    # A custom image may consume these packages or share their namespace files.
    # Repair only the isolated leftovers; uv still reports every retained conflict.
    if any(
        canonicalize_name(Requirement(raw).name) in remnants
        for distribution in retained
        for raw in distribution.requires or []
    ):
        return
    if any(installed[name].files is None for name in remnants):
        return
    removed_files = {
        installed[name].locate_file(file).resolve()
        for name in remnants
        for file in installed[name].files
    }
    for distribution in retained:
        files = distribution.files
        if files is None:
            # Distro packages may lack RECORD; preserve unrecorded owners
            # only when they can share the affected namespace.
            namespace = distribution.locate_file("opentelemetry").resolve()
            if any(path.is_relative_to(namespace) for path in removed_files):
                return
        elif any(
            distribution.locate_file(file).resolve() in removed_files for file in files
        ):
            return
    subprocess.run(
        ["uv", "pip", "uninstall", "--python", sys.executable, *sorted(remnants)],
        check=True,
    )


def check_installed_dependencies() -> None:
    """Validate the runtime with uv while retaining the engine's NCCL SDK override."""
    command = ["uv", "pip", "check", "--python", sys.executable]
    overrides = {}
    override_file = os.environ.get("UV_OVERRIDE")
    if override_file:
        for line in Path(override_file).read_text().splitlines():
            if not (raw := line.split("#", 1)[0].strip()):
                continue
            requirement = Requirement(raw)
            name = canonicalize_name(requirement.name)
            if name not in {"nvidia-nccl-cu12", "nvidia-nccl-cu13"}:
                continue
            if requirement.url is not None or not requirement.specifier:
                raise ValueError(
                    f"NCCL SDK override for {name} must use a version constraint, "
                    "not a URL or unconstrained requirement"
                )
            if requirement.marker is None or requirement.marker.evaluate():
                overrides[name] = requirement
    if not overrides:
        subprocess.run(command, check=True)
        return
    torch = importlib.metadata.distribution("torch")
    requirements = []
    # The upstream DeepEP SDK intentionally overrides PyTorch's older NCCL pin.
    # Keep all other requirements and markers; never rewrite installed METADATA.
    for raw in torch.requires or []:
        requirement = Requirement(raw)
        if override := overrides.get(canonicalize_name(requirement.name)):
            requirement.specifier = override.specifier
            raw = str(requirement)
        requirements.append(raw)
    metadata = {
        "name": torch.metadata["Name"],
        "version": torch.version,
        "requires-dist": requirements,
        "provides-extra": torch.metadata.get_all("Provides-Extra") or [],
    }
    if requires_python := torch.metadata.get("Requires-Python"):
        metadata["requires-python"] = requires_python
    with tempfile.TemporaryDirectory(prefix="foretoken-sdk-check-") as temporary:
        configuration = Path(temporary) / "uv.toml"
        configuration.write_text(
            "[[pip.dependency-metadata]]\n"
            + "\n".join(
                f"{key} = {json.dumps(value)}" for key, value in metadata.items()
            )
            + "\n"
        )
        subprocess.run(command + ["--config-file", str(configuration)], check=True)


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
