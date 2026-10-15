# SPDX-License-Identifier: Apache-2.0
# SPDX-FileCopyrightText: Copyright contributors to the Foretoken project

"""Apply vLLM runtime compatibility in image and source builds."""

from __future__ import annotations

import importlib.metadata
import importlib.util
import json
import os
import py_compile
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

from packaging.requirements import Requirement
from packaging.specifiers import SpecifierSet
from packaging.utils import canonicalize_name
from packaging.version import Version


def engine_patches(directory: Path, version: str, patches: Path) -> list[Path]:
    """Select Python engine patches in catalog order for image and source builds."""
    version_value = Version(version)
    catalog = json.loads((patches / "catalog.json").read_text())
    profile_name = catalog["default_profile"]
    for profile in catalog["profiles"]:
        if SpecifierSet(profile["versions"]).contains(version_value, prereleases=True):
            profile_name = profile["name"]
            break

    # Some legacy packages backport the modern Ready interface and use its patch family.
    ready_source = directory / "vllm/v1/engine/__init__.py"
    if profile_name == "v1-legacy" and ready_source.is_file():
        source = ready_source.read_text()
        if (
            "tensor_parallel_size: int" in source
            and "decode_context_parallel_size: int" in source
        ):
            profile_name = "v1-legacy-backport"

    selected = []
    for entry in catalog["patches"]:
        rule = entry.get("python")
        if rule is None:
            continue
        if "profiles" in rule and profile_name not in rule["profiles"]:
            continue
        if "versions" in rule and not SpecifierSet(rule["versions"]).contains(
            version_value, prereleases=True
        ):
            continue
        if "requires" in rule and not (directory / rule["requires"]).is_file():
            continue
        selected.append(patches / entry["path"])
    return selected


def patch_files(patches: list[Path]) -> set[Path]:
    """Return both sides of patch targets for private staging and payload export."""
    return {
        Path(line[6:].split()[0])
        for patch in patches
        for line in patch.read_text().splitlines()
        if line.startswith(("--- a/", "+++ b/"))
    }


def apply_patches(directory: Path, patches: list[Path]) -> set[Path]:
    """Strictly apply an ordered stack to a caller-owned private tree and return its targets."""
    for patch in patches:
        subprocess.run(
            [
                "patch",
                "--fuzz=0",
                "--strip=1",
                "--batch",
                "--forward",
                "--no-backup-if-mismatch",
                f"--directory={directory}",
                f"--input={patch.resolve()}",
            ],
            check=True,
        )
    return patch_files(patches)


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


def install_mooncake_dependencies() -> None:
    """Prepare the CUDA transfer runtime during model-server image construction."""
    import ctypes

    import torch

    if getattr(torch.version, "maca", None) or not torch.version.cuda:
        return

    # Reuse native libraries supplied by the base image, including vendor RDMA
    # installations. Only missing libraries require system package installation.
    packages = []
    for library, package in (
        ("libcurl.so.4", "libcurl4"),
        ("libibverbs.so.1", "libibverbs1"),
        ("libmlx5.so.1", "ibverbs-providers"),
    ):
        try:
            ctypes.CDLL(library)
        except OSError:
            packages.append(package)
    if packages:
        subprocess.run(["apt-get", "update"], check=True)
        subprocess.run(
            ["apt-get", "install", "-y", "--no-install-recommends", *packages],
            env={**os.environ, "DEBIAN_FRONTEND": "noninteractive"},
            check=True,
        )
        shutil.rmtree("/var/lib/apt/lists")

    package, other = "mooncake-transfer-engine", "mooncake-transfer-engine-cuda13"
    if int(torch.version.cuda.split(".", 1)[0]) >= 13:
        package, other = other, package
    command = ["uv", "pip", "install", "--no-cache", "--python", sys.executable]
    try:
        importlib.metadata.distribution(other)
    except importlib.metadata.PackageNotFoundError:
        pass
    else:
        # Both distributions own mooncake/*; uninstall before restoring the
        # selected wheel, even when both distribution records were present.
        subprocess.run(
            ["uv", "pip", "uninstall", "--python", sys.executable, other],
            check=True,
        )
        command.extend(["--reinstall-package", package])
    version = Path(__file__).with_name("mooncake.version").read_text().strip()
    subprocess.run(command + [f"{package}=={version}"], check=True)


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
    """Prepare the installed-engine stack privately, then publish only patched package files."""
    distribution = importlib.metadata.distribution("vllm")
    directory = Path(distribution.locate_file(""))
    patches = engine_patches(directory, distribution.version, Path(__file__).parent)
    files = patch_files(patches)
    staging = Path(tempfile.mkdtemp(prefix="foretoken-vllm-patches-"))
    prepared = staging / "package"
    prepared.mkdir()
    completed = False
    try:
        for name in sorted(files):
            source = directory / name
            target = prepared / name
            target.parent.mkdir(parents=True, exist_ok=True)
            if source.is_file():
                shutil.copy2(source, target)
        # Installed images have no pristine checkout. Let patch undo recognized
        # hunks in reverse stack order, including partially applied patches.
        # Only a successful replay of the entire forward stack can publish files.
        with (staging / "bootstrap.log").open("w") as log:
            for patch in reversed(patches):
                print(f"Reversing {patch}", file=log, flush=True)
                result = subprocess.run(
                    [
                        "patch",
                        "--fuzz=0",
                        "--strip=1",
                        "--force",
                        "--reverse",
                        "--no-backup-if-mismatch",
                        f"--directory={prepared}",
                        f"--input={patch.resolve()}",
                    ],
                    stdout=log,
                    stderr=subprocess.STDOUT,
                    check=False,
                )
                if result.returncode not in (0, 1):
                    result.check_returncode()
        apply_patches(prepared, patches)
        bytecode = {}
        for name in sorted(files):
            path = prepared / name
            if path.is_file() and name.suffix == ".py":
                compiled = staging / "bytecode" / name.with_suffix(".pyc")
                py_compile.compile(
                    str(path),
                    cfile=str(compiled),
                    dfile=str(directory / name),
                    doraise=True,
                )
                bytecode[compiled] = Path(
                    importlib.util.cache_from_source(str(directory / name))
                )
        for name in sorted(files):
            source = prepared / name
            target = directory / name
            if source.is_file():
                target.parent.mkdir(parents=True, exist_ok=True)
                shutil.copy2(source, target)
            else:
                target.unlink(missing_ok=True)
        for source, target in bytecode.items():
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(source, target)
        completed = True
    finally:
        if completed:
            shutil.rmtree(staging)
        else:
            print(f"vLLM patch preparation retained at {staging}", file=sys.stderr)


if __name__ == "__main__":
    main()
