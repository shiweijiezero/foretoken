# SPDX-License-Identifier: Apache-2.0
# SPDX-FileCopyrightText: Copyright contributors to the Foretoken project

"""Prepare movable engine overlays using the runtime's Python and native toolchain."""

from __future__ import annotations

import argparse
import filecmp
import importlib.metadata
import importlib.util
import json
import logging
import os
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

import torch
from build import ProjectBuilder
from engine_sources import generated_engine_file
from packaging.metadata import Metadata
from packaging.utils import canonicalize_name
from packaging.version import Version
from vllm_patches import apply_patches, engine_patches, runtime_constraints

logger = logging.getLogger(__name__)

PATCH_ROOT = Path("/opt/foretoken-engine-build/patches")
METAX_ROOT = Path("/opt/foretoken-engine-build/metax")


def main() -> None:
    """Prepare dependency inputs or complete source overlays for the selected build stage."""
    logging.basicConfig(level=logging.INFO, format="%(message)s", stream=sys.stderr, force=True)
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--source-root", type=Path, required=True)
    parser.add_argument("--cache", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--build-native", action="store_true")
    parser.add_argument("--dependencies-only", action="store_true")
    arguments = parser.parse_args()
    cache = arguments.cache.resolve()
    cache.mkdir(parents=True, exist_ok=True)
    sources = json.loads((arguments.source_root / "manifest.json").read_text())
    core = cache / "vllm"
    deleted_core = synchronize_source(
        arguments.source_root / "vllm", core, sources["vllm"]["files"]
    )
    metax = bool(getattr(torch.version, "maca", None))
    plugin_input = arguments.source_root / "vllm-metax"
    plugin = cache / "vllm-metax" if "vllm-metax" in sources else None
    deleted_plugin = (
        synchronize_source(
            plugin_input, plugin, sources["vllm-metax"]["files"]
        )
        if plugin is not None
        else set()
    )
    if plugin is not None and not metax:
        raise RuntimeError("a vllm-metax checkout requires a MetaX runtime")
    native = cache / "native-install"
    version = importlib.metadata.version("vllm")
    patched_files = prepare_patches(core, plugin, version, metax)
    output = arguments.output.resolve()
    engine = output / "engine"
    if engine.exists():
        shutil.rmtree(engine)
    engine.mkdir(parents=True)
    prepare_metadata(core, plugin, output, metax)
    if arguments.dependencies_only:
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
    export_overlay(
        core, engine, deleted_core, "vllm", cache, patched_files.get(core, set())
    )
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


def synchronize_source(
    source: Path, workspace: Path, files: list[str]
) -> set[str]:
    """Synchronize the complete source inventory without depending on Git's deletion records."""
    manifest = workspace.parent / f"{workspace.name}-files.json"
    originals = workspace.parent / f"{workspace.name}-inputs"
    previous = set(json.loads(manifest.read_text())) if manifest.exists() else set()
    previous.update(str(path.relative_to(originals)) for path in originals.rglob("*") if path.is_file())
    current = set(files)
    known = previous | current
    pending = manifest.with_suffix(".next")
    pending.write_text(json.dumps(sorted(known)) + "\n")
    pending.replace(manifest)
    for name in sorted(previous - current, reverse=True):
        path = originals / name
        if path.is_file() or path.is_symlink():
            path.unlink()
        parent = path.parent
        while parent != originals and parent.is_dir() and not any(parent.iterdir()):
            parent.rmdir()
            parent = parent.parent
    for name in sorted(current):
        origin, original = source / name, originals / name
        if (
            not original.is_file()
            or not filecmp.cmp(origin, original, shallow=False)
            or origin.stat().st_mode != original.stat().st_mode
        ):
            if original.is_dir():
                shutil.rmtree(original)
            original.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(origin, original)
    return known - current


def prepare_patches(
    core: Path, plugin: Path | None, version: str, metax: bool
) -> dict[Path, set[str]]:
    """Validate complete patch stacks, then publish owned sources without deleting generated files."""
    directories = [core] if plugin is None else [core, plugin]
    staging = Path(tempfile.mkdtemp(prefix="engine-patches-", dir=core.parent))
    completed = False
    try:
        patches: dict[Path, list[Path]] = {directory: [] for directory in directories}
        for directory in directories:
            originals = directory.parent / f"{directory.name}-inputs"
            shutil.copytree(originals, staging / directory.name)
        if metax:
            configuration = json.loads(
                (METAX_ROOT / "source-environment.json").read_text()
            )
            # SDK backports target the manifest's core version. Custom runtimes
            # provide their own matching SDK adaptations.
            if (
                Version(version).base_version
                == Version(configuration["core"]["version"]).base_version
            ):
                for target, directory in (("core", core), ("metax", plugin)):
                    if directory is not None:
                        patches[directory].extend(
                            METAX_ROOT / name
                            for name in configuration["patches"][target]
                        )
        patches[core].extend(engine_patches(staging / core.name, version, PATCH_ROOT))
        patched_files = {
            directory: {
                str(name)
                for name in apply_patches(staging / directory.name, patches[directory])
            }
            for directory in directories
        }
        # CMake also generates files inside the source tree. Publish only captured
        # inputs and patch outputs, retaining generated files and unchanged mtimes.
        for directory in directories:
            prepared = staging / directory.name
            manifest = directory.parent / f"{directory.name}-prepared-files.json"
            inputs = directory.parent / f"{directory.name}-files.json"
            previous = set(json.loads(inputs.read_text()))
            if manifest.exists():
                previous.update(json.loads(manifest.read_text()))
            else:
                # Trees prepared before file ownership was recorded may have lost
                # generated sources. Reconfigure once without discarding objects.
                (directory.parent / "native-build/CMakeCache.txt").unlink(missing_ok=True)
            current = {
                str(path.relative_to(prepared))
                for path in prepared.rglob("*") if path.is_file()
            }
            # Record every possible publication before writes, so a retry can also
            # remove files introduced by an interrupted, subsequently changed stack.
            pending = staging / manifest.name
            pending.write_text(json.dumps(sorted(previous | current)) + "\n")
            pending.replace(manifest)
            for name in sorted(previous - current, reverse=True):
                path = directory / name
                if path.is_file() or path.is_symlink():
                    path.unlink()
                parent = path.parent
                while parent != directory and parent.is_dir() and not any(parent.iterdir()):
                    parent.rmdir()
                    parent = parent.parent
            for name in sorted(current):
                origin, target = prepared / name, directory / name
                if (
                    target.is_file()
                    and target.stat().st_mode == origin.stat().st_mode
                    and filecmp.cmp(origin, target, shallow=False)
                ):
                    continue
                if target.is_dir():
                    target.rmdir()
                target.parent.mkdir(parents=True, exist_ok=True)
                # Restoring an older input must still invalidate compiled consumers.
                origin.touch()
                origin.replace(target)
            pending.write_text(json.dumps(sorted(current)) + "\n")
            pending.replace(manifest)
        completed = True
        return patched_files
    finally:
        if completed:
            shutil.rmtree(staging)
        else:
            logger.warning("engine patch preparation retained at %s", staging)


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
    )
    environment.setdefault("CCACHE_DIR", str(cache / "ccache"))
    if getattr(torch.version, "maca", None):
        # The SDK CMake wrapper must preserve failures before its cleanup commands.
        environment["BASH_ERREXIT"] = "1"
    elif torch.version.cuda:
        # Runtime wheels can supply CUDA library headers outside CUDA_HOME.
        # System include paths keep explicit toolkit paths ahead of wheel headers.
        includes = set()
        for distribution in importlib.metadata.distributions():
            if not canonicalize_name(distribution.metadata["Name"]).startswith("nvidia-"):
                continue
            for file in distribution.files or ():
                if "include" in file.parts:
                    directory = Path(*file.parts[:file.parts.index("include") + 1])
                    includes.add(str(distribution.locate_file(directory)))
        if includes:
            environment["CPLUS_INCLUDE_PATH"] = os.pathsep.join(
                filter(None, (environment.get("CPLUS_INCLUDE_PATH"), *sorted(includes)))
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
    if source is not None and not (source / package / "__init__.py").is_file():
        # A namespace package would let Python select the image's old regular package.
        raise RuntimeError(f"engine source must provide {package}/__init__.py")
    runtime = package_directory(package)
    # Keep package assets, including hidden native libraries, but not repository metadata.
    excluded = {
        ".git",
        ".gitkeep",
        ".gitignore",
        ".gitattributes",
        ".gitmodules",
        "__pycache__",
    }
    # Source owns Python modules and package resources, including vendored code.
    # The image supplies native libraries and generated version metadata.
    for path in runtime.rglob("*"):
        relative = path.relative_to(runtime)
        if not path.is_file() or any(part in excluded for part in relative.parts):
            continue
        if generated_engine_file(Path(package) / relative):
            target = destination / package / relative
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(path, target)
    native = cache / "native-install"
    if (native / package).is_dir():
        shutil.copytree(
            native / package,
            destination / package,
            dirs_exist_ok=True,
            ignore=shutil.ignore_patterns(*excluded),
        )
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
            if any(part in excluded for part in Path(name).parts):
                continue
            target = destination / name
            if target.is_dir():
                shutil.rmtree(target)
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(source / name, target)
    for name in deleted:
        path = destination / name
        if path.is_file() or path.is_symlink():
            path.unlink()


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


def prepare_metadata(
    core: Path, plugin: Path | None, output: Path, metax: bool
) -> None:
    """Export upstream distribution metadata and dependency inputs without packaging engine code."""
    projects = [(core, "vllm")]
    if plugin is not None:
        projects.append((plugin, "vllm_metax"))
    engine_names = {canonicalize_name(package) for _, package in projects}
    constraints = runtime_constraints(engine_names)
    dependencies = output / "dependencies"
    dependencies.mkdir(parents=True, exist_ok=True)
    (dependencies / "runtime-native-constraints.txt").write_text(
        "".join(
            f"{name}==={version}\n" for name, version in sorted(constraints.items())
        )
    )
    requirements = set()
    for source, package in projects:
        with tempfile.TemporaryDirectory(
            prefix="foretoken-engine-metadata-"
        ) as temporary:
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
                VLLM_USE_PRECOMPILED="0",
                VLLM_USE_PRECOMPILED_RUST="0",
                USE_PRECOMPILED_KERNEL="1",
            )
            if metax:
                environment["VLLM_TARGET_DEVICE"] = "empty" if package == "vllm" else "cuda"

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
            metadata = Metadata.from_email((dist_info / "METADATA").read_bytes(), validate=False)
            for requirement in metadata.requires_dist or ():
                if requirement.marker is not None and not requirement.marker.evaluate({"extra": ""}):
                    continue
                name = canonicalize_name(requirement.name)
                # The bound engines are delivered as files, not resolver candidates.
                if name in engine_names:
                    continue
                if requirement.url and name in constraints:
                    origin = importlib.metadata.distribution(name).read_text("direct_url.json")
                    if origin is None or json.loads(origin)["url"] != requirement.url:
                        raise RuntimeError(
                            f"{requirement.name} is supplied by the runtime image; "
                            "a different runtime dependency source requires a matching base image"
                        )
                    # Keep the installed wheel; resolve any requested extras against its pinned version.
                    if not requirement.extras:
                        continue
                    requirement.url = None
                requirements.add(str(requirement))
            destination = output / "engine" / dist_info.name
            shutil.copytree(dist_info, destination)
            # Reused or rebuilt native outputs retain this interpreter and accelerator ABI.
            wheel_metadata = distribution.read_text("WHEEL")
            if wheel_metadata is None:
                raise RuntimeError(
                    f"runtime {package} distribution has no WHEEL metadata"
                )
            (destination / "WHEEL").write_text(wheel_metadata)
    (dependencies / "requirements.txt").write_text(
        "".join(f"{requirement}\n" for requirement in sorted(requirements))
    )


if __name__ == "__main__":
    main()
