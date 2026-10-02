# SPDX-License-Identifier: Apache-2.0
# SPDX-FileCopyrightText: Copyright contributors to the Foretoken project

"""Prepare movable engine overlays using the runtime's Python and native toolchain."""

from __future__ import annotations

import argparse
import filecmp
import importlib.metadata
import importlib.util
import json
import os
import shutil
import subprocess
import sys
import tempfile
from email.parser import Parser
from pathlib import Path

import torch
from build import ProjectBuilder
from packaging.requirements import Requirement
from packaging.utils import canonicalize_name
from packaging.version import Version
from vllm_patches import apply_patch, engine_patches

PATCH_ROOT = Path("/opt/foretoken-engine-build/patches")
METAX_ROOT = Path("/opt/foretoken-engine-build/metax")


def synchronize_source(
    source: Path, workspace: Path, files: list[str], deleted: list[str]
) -> set[str]:
    """Update the persistent builder checkout from the CLI's authoritative source manifest."""
    manifest = workspace.parent / f"{workspace.name}-files.json"
    originals = workspace.parent / f"{workspace.name}-inputs"
    previous = set(json.loads(manifest.read_text())) if manifest.exists() else set()
    current = set(files)
    previous.update(deleted)
    for name in previous - current:
        for directory in (workspace, originals):
            path = directory / name
            if path.is_file() or path.is_symlink():
                path.unlink()
    for name in sorted(current):
        origin, original = source / name, originals / name
        if (
            not original.is_file()
            or not filecmp.cmp(origin, original, shallow=False)
            or origin.stat().st_mode != original.stat().st_mode
        ):
            # Compare unpatched inputs, not the checkout modified by upstream generation.
            for destination in (original, workspace / name):
                if destination.is_dir():
                    shutil.rmtree(destination)
                destination.parent.mkdir(parents=True, exist_ok=True)
                shutil.copy2(origin, destination)
    known = previous | current
    manifest.write_text(json.dumps(sorted(known)) + "\n")
    return known - current


def prepare_patches(
    core: Path, plugin: Path | None, version: str, metax: bool
) -> dict[Path, set[str]]:
    """Apply engine backports and return their package files for payload export."""
    patches = []
    if metax:
        configuration = json.loads((METAX_ROOT / "source-environment.json").read_text())
        for target, directory in (("core", core), ("metax", plugin)):
            if directory is not None:
                patches.extend(
                    (directory, METAX_ROOT / name)
                    for name in configuration["patches"][target]
                )
    patches.extend((core, patch) for patch in engine_patches(core, version, PATCH_ROOT))
    # A changed input may reset one file of a multi-file patch. Restore all patch
    # inputs together before applying the ordered series to avoid mixed states.
    patched_files: dict[Path, set[str]] = {}
    for directory, patch in patches:
        originals = directory.parent / f"{directory.name}-inputs"
        for line in patch.read_text().splitlines():
            if line.startswith("+++ b/"):
                name = line.split()[1].removeprefix("b/")
                patched_files.setdefault(directory, set()).add(name)
                if (originals / name).is_file():
                    shutil.copy2(originals / name, directory / name)
                else:
                    (directory / name).unlink(missing_ok=True)
    for directory, patch in patches:
        apply_patch(directory, patch)
    return patched_files


def package_directory(name: str) -> Path:
    """Locate the runtime package supplying native and generated artifacts to the overlay."""
    specification = importlib.util.find_spec(name)
    if specification is None or specification.origin is None:
        raise RuntimeError(f"runtime does not provide the {name} package")
    return Path(specification.origin).parent


def compile_native(source: Path, cache: Path, version: str) -> None:
    """Run upstream build_ext with persistent CMake objects and retain successful outputs."""
    staging = cache / "native-candidate"
    if staging.exists():
        shutil.rmtree(staging)
    staging.mkdir(parents=True)
    environment = os.environ.copy()
    environment.pop("VLLM_PRECOMPILED_WHEEL_LOCATION", None)
    environment.update(
        SETUPTOOLS_SCM_PRETEND_VERSION=version,
        VLLM_VERSION_OVERRIDE=version,
        VLLM_TARGET_DEVICE="cuda",
        VLLM_USE_PRECOMPILED="0",
        VLLM_USE_PRECOMPILED_RUST="0",
        USE_PRECOMPILED_KERNEL="0",
        FETCHCONTENT_BASE_DIR=str(cache / "dependencies"),
        CCACHE_DIR=str(cache / "ccache"),
    )
    if not environment.get("TORCH_CUDA_ARCH_LIST"):
        # Upstream CMake selects its supported architectures for CPU-only builds.
        environment.pop("TORCH_CUDA_ARCH_LIST", None)
    subprocess.run(
        [
            sys.executable,
            "setup.py",
            "build_ext",
            "--build-temp",
            str(cache / "native-build"),
            "--build-lib",
            str(staging),
        ],
        cwd=source,
        env=environment,
        check=True,
    )
    if not any(staging.rglob("*.so")):
        raise RuntimeError("upstream native build did not produce any shared libraries")
    manifest = cache / "native-files.json"
    previous = set(json.loads(manifest.read_text())) if manifest.exists() else set()
    current = {
        str(path.relative_to(staging)) for path in staging.rglob("*") if path.is_file()
    }
    installed = cache / "native-install"
    if installed.exists():
        shutil.rmtree(installed)
    staging.rename(installed)
    manifest.write_text(json.dumps(sorted(previous | current)) + "\n")


def export_overlay(
    source: Path | None,
    destination: Path,
    deleted: set[str],
    package: str,
    cache: Path,
    patched_files: set[str],
) -> None:
    """Assemble runtime files, retained native outputs and current source without image symlinks."""
    runtime = package_directory(package)
    # Ordinary Python modules come only from source. The image supplies native
    # libraries, build metadata and externally generated/vendor packages.
    for path in runtime.rglob("*"):
        if not path.is_file() or "__pycache__" in path.parts:
            continue
        relative = path.relative_to(runtime)
        if (
            path.name in {"_version.py", "vllm-rs"}
            or path.name.endswith(".so")
            or ".so." in path.name
            or relative.parts[0] in {"libs", "third_party", "vllm_flash_attn"}
        ):
            target = destination / package / relative
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(path, target)
    native = cache / "native-install"
    if (native / package).is_dir():
        shutil.copytree(native / package, destination / package, dirs_exist_ok=True)
        known = json.loads((cache / "native-files.json").read_text())
        for name in known:
            if not (native / name).is_file():
                (destination / name).unlink(missing_ok=True)
    # Source wins over generated Python files; compiled extensions remain in the payload.
    if source is not None:
        originals = source.parent / f"{source.name}-inputs"
        files = {
            str(path.relative_to(originals))
            for path in (originals / package).rglob("*")
            if path.is_file()
        }
        files.update(name for name in patched_files if name.startswith(package + "/"))
        for name in sorted(files):
            target = destination / name
            if target.is_dir():
                shutil.rmtree(target)
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(source / name, target)
    for name in deleted:
        path = destination / name
        if path.is_file() or path.is_symlink():
            path.unlink()


def runtime_constraints() -> dict[str, str]:
    """Retain the installed accelerator ABI packages while allowing ordinary dependency updates."""
    protected = {
        "torch",
        "torchvision",
        "torchaudio",
        "torchcodec",
        "triton",
        "mcoplib",
        "maca-python",
    }
    constraints = {}
    for distribution in importlib.metadata.distributions():
        name = canonicalize_name(distribution.metadata["Name"])
        if name in {"vllm", "vllm-metax"}:
            continue
        local = distribution.version.partition("+")[2].lower()
        if (
            name in protected
            or name.startswith("nvidia-")
            or "metax" in local
            or "maca" in local
        ):
            constraints[name] = distribution.version
    return constraints


def prepare_sdk_audio(plugin: Path, staging: Path) -> None:
    """Reuse the MetaX installer's audio requirement adaptation for metadata generation."""
    relative = Path("requirements/maca_private.txt")
    original = plugin.parent / f"{plugin.name}-inputs" / relative
    shutil.copy2(original, plugin / relative)
    if "torchaudio==2.4.1+metax3.8.2.2" not in original.read_text():
        return
    prefix = staging / "sdk-audio"
    (prefix / "third_party").mkdir(parents=True)
    (prefix / "third_party/vllm-metax").symlink_to(plugin, target_is_directory=True)
    subprocess.run(
        [sys.executable, str(METAX_ROOT / "sdk_audio.py"), str(prefix), sys.executable],
        check=True,
    )


def package_wheels(core: Path, plugin: Path | None, output: Path, metax: bool) -> None:
    """Package existing engine outputs and upstream metadata for full-image dependency installation."""
    constraints = runtime_constraints()
    (output / "runtime-native-constraints.txt").write_text(
        "".join(
            f"{name}==={version}\n" for name, version in sorted(constraints.items())
        )
    )
    wheels = output / "wheels"
    if wheels.exists():
        shutil.rmtree(wheels)
    wheels.mkdir()
    projects = [(core, "vllm")]
    if plugin is not None:
        projects.append((plugin, "vllm_metax"))
    for source, package in projects:
        with tempfile.TemporaryDirectory(prefix="foretoken-engine-wheel-") as temporary:
            staging = Path(temporary)
            if package == "vllm_metax":
                prepare_sdk_audio(source, staging)
            distribution = importlib.metadata.distribution(package)
            environment = os.environ.copy()
            environment.pop("VLLM_PRECOMPILED_WHEEL_LOCATION", None)
            environment.update(
                SETUPTOOLS_SCM_PRETEND_VERSION=(
                    Version(distribution.version).public
                    if package == "vllm_metax"
                    else distribution.version
                ),
                VLLM_VERSION_OVERRIDE=distribution.version,
                VLLM_TARGET_DEVICE="empty" if metax and package == "vllm" else "cuda",
                VLLM_USE_PRECOMPILED="0",
                VLLM_USE_PRECOMPILED_RUST="0",
                USE_PRECOMPILED_KERNEL="1",
            )

            def run_backend(
                command, cwd=None, extra_environ=None, *, environment=environment
            ):
                """Run the public metadata hook in the unchanged runtime Python environment."""
                subprocess.run(
                    command,
                    cwd=cwd,
                    env={**environment, **(extra_environ or {})},
                    check=True,
                )

            prepared = ProjectBuilder(
                str(source), python_executable=sys.executable, runner=run_backend
            ).prepare("wheel", str(staging / "metadata"))
            if prepared is None:
                raise RuntimeError(
                    "engine backend does not provide a metadata-only wheel hook"
                )
            dist_info = Path(prepared)
            metadata = Parser().parsestr((dist_info / "METADATA").read_text())
            for value in metadata.get_all("Requires-Dist", []):
                requirement = Requirement(value)
                if (
                    requirement.url
                    and canonicalize_name(requirement.name) in constraints
                    and (
                        requirement.marker is None
                        or requirement.marker.evaluate({"extra": ""})
                    )
                ):
                    raise RuntimeError(
                        f"{requirement.name} is supplied by the runtime image; "
                        "a different accelerator package source requires a matching base image"
                    )
            root = staging / "wheel"
            root.mkdir()
            shutil.copytree(output / "engine" / package, root / package)
            destination = root / dist_info.name
            shutil.copytree(dist_info, destination)
            wheel_metadata = distribution.read_text("WHEEL")
            if wheel_metadata is None:
                raise RuntimeError(
                    f"runtime {package} distribution has no WHEEL metadata"
                )
            # The wheel stays inside this image build and targets the same runtime ABI.
            (destination / "WHEEL").write_text(wheel_metadata)
            subprocess.run(
                [
                    sys.executable,
                    "-m",
                    "wheel",
                    "pack",
                    str(root),
                    "--dest-dir",
                    str(wheels),
                ],
                check=True,
            )


def main() -> None:
    """Prepare source payloads or metadata-complete wheels for the image build's selected stage."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--source-root", type=Path, required=True)
    parser.add_argument("--cache", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--build-native", action="store_true")
    parser.add_argument("--package-wheels", action="store_true")
    arguments = parser.parse_args()
    cache = arguments.cache.resolve()
    cache.mkdir(parents=True, exist_ok=True)
    sources = json.loads((arguments.source_root / "manifest.json").read_text())
    deleted = json.loads((arguments.source_root / "deleted.json").read_text())
    core = cache / "vllm"
    deleted_core = synchronize_source(
        arguments.source_root / "vllm", core, sources["vllm"]["files"], deleted["vllm"]
    )
    metax = bool(getattr(torch.version, "maca", None))
    plugin_input = arguments.source_root / "vllm-metax"
    plugin = cache / "vllm-metax" if "vllm-metax" in sources else None
    deleted_plugin = (
        synchronize_source(
            plugin_input, plugin, sources["vllm-metax"]["files"], deleted["vllm-metax"]
        )
        if plugin is not None
        else set()
    )
    if plugin is not None and not metax:
        raise RuntimeError("a vllm-metax checkout requires a MetaX runtime")
    version = importlib.metadata.version("vllm")
    patched_files = prepare_patches(core, plugin, version, metax)
    if arguments.package_wheels:
        output = arguments.output.resolve()
        if plugin is None and (output / "engine/vllm_metax").is_dir():
            plugin = cache / "vllm-metax"
        package_wheels(core, plugin, output, metax)
        return
    if arguments.build_native:
        if metax and plugin is None:
            raise RuntimeError(
                "MetaX native updates require a vllm-metax plugin checkout; "
                "core CUDA sources are not MACA kernels"
            )
        if not metax and (torch.version.cuda is None or torch.version.hip is not None):
            raise RuntimeError(
                "native source builds support NVIDIA CUDA and the MetaX plugin"
            )
        compile_native(
            plugin if metax else core,
            cache,
            importlib.metadata.version("vllm-metax") if metax else version,
        )
    output = arguments.output.resolve()
    engine = output / "engine"
    if engine.exists():
        shutil.rmtree(engine)
    engine.mkdir(parents=True)
    export_overlay(
        core, engine, deleted_core, "vllm", cache, patched_files.get(core, set())
    )
    native = cache / "native-install"
    if metax and native.exists() and plugin is None:
        plugin = cache / "vllm-metax"
        known = json.loads((cache / "vllm-metax-files.json").read_text())
        originals = cache / "vllm-metax-inputs"
        deleted_plugin = {name for name in known if not (originals / name).is_file()}
    if plugin is not None:
        export_overlay(
            plugin,
            engine,
            deleted_plugin,
            "vllm_metax",
            cache,
            patched_files.get(plugin, set()),
        )
    environment = {}
    if metax:
        # An image may already activate its own compiled plugin. Source updates
        # retain that selection even when this binding has no new native build.
        value = os.environ.get("USE_PRECOMPILED_KERNEL")
        directory = os.environ.get("FORETOKEN_ENGINE_DIRECTORY")
        if directory:
            installed = json.loads(
                (Path(directory) / "engine-environment.json").read_text()
            )
            value = installed.get("USE_PRECOMPILED_KERNEL", value)
        if native.exists():
            value = "0"
        if value is not None:
            environment["USE_PRECOMPILED_KERNEL"] = value
    (output / "engine-environment.json").write_text(json.dumps(environment) + "\n")


if __name__ == "__main__":
    main()
