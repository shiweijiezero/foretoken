# SPDX-License-Identifier: Apache-2.0
# SPDX-FileCopyrightText: Copyright contributors to the Foretoken project

"""Select, compare and capture source directories for installation and deployment."""

from __future__ import annotations

import filecmp
import json
import os
import shutil
import stat
import subprocess
import sys
import tomllib
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from foretoken.build.engine_sources import generated_engine_file
from foretoken.errors import DeploymentError
from foretoken.working_directory import find_source_directory

_SOURCE_METADATA = ".foretoken-sources.json"
SNAPSHOT_METADATA = frozenset({"engine/manifest.json", _SOURCE_METADATA})
_RUST_SOURCE = "data-plane/third_party/vllm"
_GENERATED_DIRECTORIES = frozenset({
    ".git", ".venv", ".gpu_cache", ".cache", "__pycache__", ".pytest_cache", ".mypy_cache", ".ruff_cache",
    ".tox", ".nox", ".hypothesis", ".ipynb_checkpoints", "target",
})


@dataclass
class BuildInputs:
    """Own source files and declared dependencies through comparison and capture."""

    root: Path
    engines: dict[str, str]
    files: dict[str, Path]
    dependencies: dict[str, dict[str, Any]]
    mooncake_version: str | None = None

    @classmethod
    def select(
        cls, root: Path, engines: dict[str, str] | None = None,
        *, mooncake_version: str | None = None,
    ) -> BuildInputs:
        """Discover build inputs from directories, independently of their version-control metadata."""
        files = {}
        for prefix, excluded in (
            ("data-plane", {"third_party/vllm"}),
            ("control-plane", {"bin", "cover.out"}),
            ("deploy/charts/foretoken", set()),
            ("deploy/inference-engines", set()),
            ("deploy/mooncake", set()),
        ):
            directory = root / prefix
            if not directory.is_dir():
                continue
            for name, path in _directory_files(directory, excluded).items():
                name = prefix + "/" + name
                if path.suffix not in {".md", ".png", ".svg"}:
                    files[name] = path
        for name in ("LICENSE", ".dockerignore", "deploy/build-source", "cli/foretoken/build/engine_sources.py"):
            path = root / name
            if path.is_file():
                files[name] = path
        revision = pinned_rust_revision(root)
        files.update(_rust_inputs(root))
        dependencies = {_RUST_SOURCE: {"revision": revision}}
        if mooncake_version is not None:
            dependency_files, dependency = mooncake_inputs(root, mooncake_version)
            files.update(dependency_files)
            dependencies["third_party/mooncake"] = dependency
        for engine, directory in (engines or {}).items():
            for name, path in _directory_files(Path(directory), {"build", "dist", ".deps"}).items():
                if not generated_engine_file(Path(name)):
                    files[f"engine/{engine}/{name}"] = path
        return cls(root, engines or {}, files, dependencies, mooncake_version)

    def changed(self, snapshot: Path) -> set[str]:
        """Return changed file and dependency paths for build planning or validation."""
        previous = {
            str(path.relative_to(snapshot))
            for path in snapshot.rglob("*")
            if path.is_file()
            and str(path.relative_to(snapshot)) not in SNAPSHOT_METADATA
        }
        changed = {
            name for name in previous | self.files.keys()
            if name not in previous or name not in self.files
            or not _same_source_file(snapshot / name, self.files[name])
        }
        dependencies = _snapshot_dependencies(snapshot)
        for name in dependencies.keys() | self.dependencies.keys():
            if dependencies.get(name) != self.dependencies.get(name):
                changed.add(name)
        return changed

    def validate(self, snapshot: Path) -> None:
        """Reject source changes during preparation before publishing captured artifacts."""
        if self.changed(snapshot):
            raise DeploymentError("source changed while preparing artifacts; rerun the source operation")

    def capture(
        self, destination: Path, previous: Path | None = None,
        unchanged: set[str] | None = None,
    ) -> None:
        """Capture complete source inventories and optional provenance for a build operation."""
        destination.mkdir(parents=True, mode=0o700)
        for name, source in self.files.items():
            target = destination / name
            target.parent.mkdir(parents=True, exist_ok=True)
            prior = previous / name if previous is not None else None
            mode = _source_file_mode(source)
            if (
                prior is not None and unchanged is not None and name in unchanged
                and stat.S_IMODE(prior.stat().st_mode) == mode
            ):
                os.link(prior, target)
            else:
                shutil.copyfile(source, target)
                target.chmod(mode)
        metadata = {
            _SOURCE_METADATA: {
                "foretoken": _checkout_source(self.root),
                "engines": {name: _checkout_source(Path(directory)) for name, directory in self.engines.items()},
                "dependencies": self.dependencies,
            },
        }
        if self.engines:
            metadata["engine/manifest.json"] = {
                engine: {"files": sorted(
                    name.removeprefix(f"engine/{engine}/")
                    for name in self.files if name.startswith(f"engine/{engine}/")
                )}
                for engine in self.engines
            }
        for name, value in metadata.items():
            path = destination / name
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(json.dumps(value, indent=2) + "\n")
        type(self).select(
            self.root, self.engines, mooncake_version=self.mooncake_version,
        ).validate(destination)


def source_checkout(explicit: str | None = None) -> Path:
    """Find the selected Foretoken source root using its build files, not Git metadata."""
    root = Path(explicit).expanduser().resolve() if explicit is not None else Path.cwd().resolve()
    directory = find_source_directory(root)
    if directory is None or explicit is not None and directory != root:
        raise DeploymentError(f"not a Foretoken source directory: {root}; pass --editable PATH")
    return directory


def engine_source_roots(sources: tuple[str, ...]) -> dict[str, str]:
    """Resolve explicitly selected engine directories without requiring version control."""
    roots = {}
    for source in sources:
        name, separator, value = source.partition("=")
        if not separator:
            name, value = "vllm", source
        if name not in {"vllm", "vllm-metax"} or name in roots:
            raise DeploymentError("--engine-source accepts one vllm directory and one optional vllm-metax directory")
        path = Path(value).expanduser().resolve()
        if not (path / name.replace("-", "_")).is_dir() or not (path / "setup.py").is_file():
            raise DeploymentError(f"engine source is not a {name} directory: {path}")
        roots[name] = str(path)
    if roots and "vllm" not in roots:
        raise DeploymentError("--engine-source vllm-metax=PATH also requires the corresponding --engine-source vllm=PATH")
    return roots


def pinned_rust_revision(root: Path) -> str:
    """Read the declared Rust dependency revision from the source directory's Cargo metadata."""
    metadata = tomllib.loads((root / "data-plane/Cargo.toml").read_text())["workspace"]["metadata"]["foretoken"]
    revision = metadata.get("vllm_revision")
    if not isinstance(revision, str) or not revision:
        raise DeploymentError("data-plane/Cargo.toml must declare workspace.metadata.foretoken.vllm_revision")
    return revision


def mooncake_build_version(
    root: Path, arguments: dict[str, str], *, runtime_backend: str, build_metax_runtime: bool,
) -> str | None:
    """Select the native source recipe used by CUDA or managed MetaX environment builds."""
    if build_metax_runtime:
        return arguments.get("MOONCAKE_VERSION", "")
    if runtime_backend != "metax":
        return (root / "data-plane/patches/vllm/mooncake.version").read_text().strip()
    return None


def mooncake_inputs(
    root: Path, version: str,
) -> tuple[dict[str, Path], dict[str, Any]]:
    """Capture supplied Mooncake files and the declared recipe; the build executor prepares dependencies."""
    prefix = "third_party/mooncake"
    preparer = root / "deploy/mooncake/prepare-source"
    environment = {**os.environ, "MOONCAKE_VERSION": version}
    described = subprocess.run(
        [sys.executable, "-I", str(preparer), "--describe"], cwd=root, env=environment,
        capture_output=True, text=True, check=False,
    )
    if described.returncode:
        raise DeploymentError(described.stderr.strip() or described.stdout.strip())
    recipe = json.loads(described.stdout)
    dependency = {
        "revision": recipe["revision"], "patches": recipe["patches"],
        "dependencies": {name: value["revision"] for name, value in recipe["dependencies"].items()},
    }
    directory = root / prefix
    files = {} if _empty_source_directory(directory) else {
        prefix + "/" + name: path
        for name, path in _directory_files(directory, {"build", "dist", ".deps"}).items()
    }
    return files, dependency


def build_sources(snapshot: Path, component: str) -> dict[str, Any] | None:
    """Read publication provenance, preserving source archives for directories without Git."""
    metadata = snapshot / _SOURCE_METADATA
    if not metadata.is_file():
        return None
    sources = json.loads(metadata.read_text())
    sources.pop("dependencies", None)
    if component != "model-server":
        sources.pop("engines", None)
    return sources


def snapshot_versions(
    snapshot: Path, previous: Path | None, versions: dict[str, str],
    changed: set[str] | None = None,
) -> dict[str, str]:
    """Preserve unchanged input versions for cluster-side delta transfer."""
    revision = snapshot.name.removeprefix("inputs-")
    result = {}
    for path in snapshot.rglob("*"):
        if not path.is_file():
            continue
        name = str(path.relative_to(snapshot))
        old = previous / name if previous else None
        unchanged = (
            name in versions and name not in changed
            if changed is not None and name not in SNAPSHOT_METADATA
            else name in versions and old is not None and old.is_file() and _same_source_file(old, path)
        )
        result[name] = versions[name] if unchanged else revision
    return result


def _directory_files(root: Path, excluded: set[str]) -> dict[str, Path]:
    """Enumerate source files, following directory aliases without accepting recursive links."""
    files = {}

    def visit(directory: Path, ancestors: frozenset[Path]) -> None:
        resolved = directory.resolve()
        if resolved in ancestors:
            raise DeploymentError(f"recursive directory link in source: {directory}")
        for path in sorted(directory.iterdir()):
            name = str(path.relative_to(root))
            if (
                name in excluded or path.name in _GENERATED_DIRECTORIES
                or path.name.endswith((".egg-info", ".dist-info")) or path.name == ".env" or path.name.startswith(".env.")
            ):
                continue
            mode = path.stat().st_mode
            if stat.S_ISDIR(mode):
                visit(path, ancestors | {resolved})
            elif stat.S_ISREG(mode) and path.name != ".DS_Store" and path.suffix not in {".pyc", ".pyo"}:
                files[name] = path

    visit(root, frozenset())
    return files


def _rust_inputs(root: Path) -> dict[str, Path]:
    """Capture a supplied Rust tree in full; absent sources use the declared dependency version."""
    directory = root / _RUST_SOURCE
    if _empty_source_directory(directory):
        return {}
    if not (directory / "rust/Cargo.toml").is_file():
        raise DeploymentError(f"vLLM source is missing rust/Cargo.toml: {directory}")
    files = {
        _RUST_SOURCE + "/rust/" + name: path
        for name, path in _directory_files(directory / "rust", set()).items()
    }
    state = directory / ".foretoken-patches.json"
    if state.is_file():
        files[_RUST_SOURCE + "/" + state.name] = state
    return files


def _empty_source_directory(path: Path) -> bool:
    """Allow automatic dependency preparation only when no source files were supplied."""
    return (not path.exists() and not path.is_symlink()) or path.is_dir() and not any(
        entry.name not in {".git", ".DS_Store"} for entry in path.iterdir()
    )


def _snapshot_dependencies(snapshot: Path) -> dict[str, dict[str, Any]]:
    """Read declared dependencies from a completed input snapshot."""
    metadata = snapshot / _SOURCE_METADATA
    if not metadata.is_file():
        return {}
    return json.loads(metadata.read_text()).get("dependencies", {})


def _source_file_mode(path: Path) -> int:
    """Preserve executable-file semantics in portable source snapshots."""
    return 0o755 if path.stat().st_mode & stat.S_IXUSR else 0o644


def _same_source_file(left: Path, right: Path) -> bool:
    """Compare source bytes and executable state independently of filesystem timestamps."""
    filecmp.clear_cache()
    return _source_file_mode(left) == _source_file_mode(right) and filecmp.cmp(left, right, shallow=False)


def _checkout_source(root: Path) -> dict[str, Any] | None:
    """Record optional Git provenance only when this directory is the repository root."""
    if not (root / ".git").exists():
        return None
    try:
        top = subprocess.run(
            ["git", "-C", str(root), "rev-parse", "--show-toplevel"], capture_output=True, text=True, check=False,
        )
    except FileNotFoundError:
        return None
    if top.returncode or Path(top.stdout.strip()).resolve() != root.resolve():
        return None
    result = subprocess.run(["git", "-C", str(root), "rev-parse", "--verify", "HEAD"], capture_output=True, text=True, check=False)
    if result.returncode:
        return None
    changed = subprocess.run(
        ["git", "-C", str(root), "status", "--porcelain", "--untracked-files=all"], capture_output=True, text=True, check=False,
    )
    if changed.returncode:
        return None
    return {"commit": result.stdout.strip(), "dirty": bool(changed.stdout)}
