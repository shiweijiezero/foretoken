# SPDX-License-Identifier: Apache-2.0
# SPDX-FileCopyrightText: Copyright contributors to the Foretoken project

"""Select, compare and capture source inputs for installation and deployment."""

from __future__ import annotations

import filecmp
import json
import os
import shutil
import stat
import subprocess
import tempfile
import tomllib
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from foretoken.errors import DeploymentError

_SOURCE_METADATA = ".foretoken-sources.json"
SNAPSHOT_METADATA = frozenset({"engine/manifest.json", "engine/deleted.json", _SOURCE_METADATA})
# Workstation entrypoints and explicitly applied values are not cluster build inputs.
_NON_BUILD_INPUTS = frozenset({"Makefile", "deploy/dev-build", "deploy/platform-values.yaml"})
_RUST_SOURCE = "data-plane/third_party/vllm"


@dataclass
class BuildInputs:
    """Own selected files and dependency provenance through comparison and capture."""

    root: Path
    engines: dict[str, str]
    files: dict[str, Path]
    dependencies: dict[str, dict[str, Any]]
    mooncake_version: str | None = None

    @classmethod
    def select(
        cls, root: Path, engines: dict[str, str] | None = None,
        previous: Path | None = None, *, mooncake_version: str | None = None,
    ) -> BuildInputs:
        """Select local build inputs, reusing only reproducible fixed dependencies."""
        files = {}
        for name in _git_names(root):
            if not name.startswith(("data-plane/", "control-plane/", "deploy/")) and name not in {
                "Makefile", "LICENSE", ".dockerignore", ".gitmodules",
            }:
                continue
            path = root / name
            if name not in _NON_BUILD_INPUTS and path.is_file() and path.suffix not in {".md", ".png", ".svg"}:
                files[name] = path
        revision = pinned_rust_revision(root)
        files.update(_rust_inputs(root, revision))
        dependencies = {_RUST_SOURCE: {"revision": revision}}
        if mooncake_version is not None:
            dependency_files, dependency = mooncake_inputs(root, previous, mooncake_version)
            files.update(dependency_files)
            dependencies["third_party/mooncake"] = dependency
        elif (root / "third_party/mooncake/.git").exists():
            dependency_files, _, _ = _git_dependency_inputs(root, "third_party/mooncake")
            files.update(dependency_files)
        for engine, checkout in (engines or {}).items():
            for name in _git_names(Path(checkout)):
                path = Path(checkout) / name
                if path.is_file():
                    files[f"engine/{engine}/{name}"] = path
        return cls(root, engines or {}, files, dependencies, mooncake_version)

    def changed(self, snapshot: Path) -> set[str]:
        """Return changed file and dependency paths for build planning or validation."""
        previous = {
            str(path.relative_to(snapshot))
            for path in snapshot.rglob("*")
            if path.is_file()
            and str(path.relative_to(snapshot)) not in SNAPSHOT_METADATA | _NON_BUILD_INPUTS
        }
        changed = {
            name for name in previous | self.files.keys()
            if name not in previous or name not in self.files
            or not _same_source_file(snapshot / name, self.files[name])
        }
        dependencies = _snapshot_dependencies(snapshot)
        for name in dependencies.keys() | self.dependencies.keys():
            old = {key: value for key, value in dependencies.get(name, {}).items() if key != "prepared"}
            new = {key: value for key, value in self.dependencies.get(name, {}).items() if key != "prepared"}
            if old != new:
                changed.add(name)
        return changed

    def validate(self, snapshot: Path) -> None:
        """Reject changed bytes or lost fixed-source provenance before publication."""
        lost_preparation = any(
            recorded.get("prepared") and not self.dependencies.get(name, {}).get("prepared")
            for name, recorded in _snapshot_dependencies(snapshot).items()
        )
        if self.changed(snapshot) or lost_preparation:
            raise DeploymentError("source changed while preparing artifacts; rerun the source operation")

    def capture(
        self, destination: Path, previous: Path | None = None,
        unchanged: set[str] | None = None,
    ) -> None:
        """Write a private snapshot for the caller to adopt after successful preparation."""
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
                "engines": {name: _checkout_source(Path(checkout)) for name, checkout in self.engines.items()},
                "dependencies": self.dependencies,
            },
        }
        if self.engines:
            manifest, deleted = {}, {}
            for engine, checkout in self.engines.items():
                prefix = f"engine/{engine}/"
                manifest[engine] = {"files": sorted(
                    name.removeprefix(prefix) for name in self.files if name.startswith(prefix)
                )}
                result = subprocess.check_output(["git", "-C", checkout, "ls-files", "--deleted", "-z"])
                deleted[engine] = [os.fsdecode(name) for name in result.split(b"\0") if name]
            metadata.update({"engine/manifest.json": manifest, "engine/deleted.json": deleted})
        for name, value in metadata.items():
            path = destination / name
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(json.dumps(value, indent=2) + "\n")
        # Recheck the original reference; a new snapshot must not certify itself.
        type(self).select(
            self.root, self.engines, previous, mooncake_version=self.mooncake_version,
        ).validate(destination)


def _git_names(root: Path, *paths: str) -> list[str]:
    """List tracked and non-ignored files for every source input selector."""
    result = subprocess.run(
        ["git", "-C", str(root), "ls-files", "-z", "--cached", "--others", "--exclude-standard", "--", *paths],
        capture_output=True, check=False,
    )
    if result.returncode:
        raise DeploymentError(result.stderr.decode().strip())
    return [os.fsdecode(name) for name in result.stdout.split(b"\0") if name]


def source_checkout(explicit: str | None = None) -> Path:
    """Resolve and validate the Foretoken checkout selected for installation or deployment."""
    if explicit is not None:
        root = Path(explicit).expanduser().resolve()
    else:
        result = subprocess.run(
            ["git", "rev-parse", "--show-toplevel"],
            capture_output=True, text=True, check=False,
        )
        if result.returncode:
            raise DeploymentError(
                "source deployment requires a Foretoken checkout; run from that checkout or pass --editable PATH"
            )
        root = Path(result.stdout.strip()).resolve()
    if not (root / "Makefile").is_file() or not (root / "deploy/charts/foretoken/Chart.yaml").is_file():
        raise DeploymentError(f"not a Foretoken source checkout: {root}")
    return root


def engine_source_roots(sources: tuple[str, ...]) -> dict[str, str]:
    """Resolve explicitly selected runtime checkouts independently of the pinned Rust source."""
    roots = {}
    for source in sources:
        name, separator, value = source.partition("=")
        if not separator:
            name, value = "vllm", source
        if name not in {"vllm", "vllm-metax"} or name in roots:
            raise DeploymentError("--engine-source accepts one vllm checkout and one optional vllm-metax checkout")
        path = Path(value).expanduser().resolve()
        if not (path / name.replace("-", "_")).is_dir() or not (path / "setup.py").is_file():
            raise DeploymentError(f"engine source is not a {name} checkout: {path}")
        roots[name] = str(path)
    if roots and "vllm" not in roots:
        raise DeploymentError("--engine-source vllm-metax=PATH also requires the corresponding --engine-source vllm=PATH")
    return roots


def pinned_rust_revision(root: Path) -> str:
    """Read the recorded Rust dependency revision without downloading its source."""
    result = subprocess.run(
        ["git", "-C", str(root), "ls-files", "--stage", "--", _RUST_SOURCE],
        capture_output=True, text=True, check=False,
    )
    fields = result.stdout.split()
    if result.returncode or len(fields) != 4 or fields[0] != "160000":
        raise DeploymentError("source installation requires the recorded vLLM Git submodule")
    return fields[1]


def _rust_inputs(root: Path, revision: str) -> dict[str, Path]:
    """Capture Rust overrides; builders restore unchanged upstream and managed patches."""
    checkout = root / _RUST_SOURCE
    if not (checkout / ".git").exists():
        if not _unmaterialized_submodule(checkout):
            raise DeploymentError(f"uninitialized dependency contains local files: {_RUST_SOURCE}")
        return {}
    if not (checkout / "rust/Cargo.toml").is_file():
        raise DeploymentError("initialized vLLM source is missing rust/Cargo.toml; restore it or deinitialize the submodule")
    files = {
        name: checkout / name for name in _git_names(checkout, "rust")
        if (checkout / name).is_file()
    }
    state = checkout / ".foretoken-patches.json"
    if state.is_file():
        stacks = [json.loads(state.read_text())]
    else:
        metadata = tomllib.loads((root / "data-plane/Cargo.toml").read_text())["workspace"]["metadata"]["foretoken"]
        catalog = root / "data-plane" / metadata["vllm_patch_catalog"]
        patches = json.loads(catalog.read_text())["patches"]
        stacks = [[], [
            {"contents": (catalog.parent / patch["path"]).read_text()}
            for patch in patches if patch.get("rust", False)
        ]]
    # Use the parent's pin, not a possibly modified local commit. Private indexes
    # inspect actual files even when the working index hides changes.
    with tempfile.TemporaryDirectory(prefix="foretoken-rust-inputs-") as temporary:
        environment = {**os.environ, "GIT_INDEX_FILE": str(Path(temporary) / "index")}
        for stack in stacks:
            loaded = subprocess.run(
                ["git", "-C", str(checkout), "read-tree", revision], env=environment,
                capture_output=True, check=False,
            )
            if loaded.returncode:
                break
            for patch in stack:
                applied = subprocess.run(
                    ["git", "-C", str(checkout), "apply", "--cached", "--include=rust/**"],
                    input=patch["contents"], env=environment,
                    capture_output=True, text=True, check=False,
                )
                if applied.returncode:
                    break
            else:
                if _matches_rust_index(checkout, environment, files):
                    return {}
    if state.is_file():
        files[state.name] = state
    return {f"{_RUST_SOURCE}/{name}": path for name, path in files.items()}


def _matches_rust_index(checkout: Path, environment: dict[str, str], files: dict[str, Path]) -> bool:
    """Compare actual Rust bytes with a private index, without Git's clean filters."""
    entries = subprocess.check_output(
        ["git", "-C", str(checkout), "ls-files", "--stage", "-z", "--", "rust"], env=environment,
    )
    expected = {}
    for entry in entries.split(b"\0"):
        if entry:
            metadata, name = entry.split(b"\t", 1)
            mode, revision, _ = metadata.split()
            if mode not in {b"100644", b"100755"}:
                return False
            expected[os.fsdecode(name)] = (mode, revision)
    if expected.keys() != files.keys():
        return False
    contents = subprocess.check_output(
        ["git", "-C", str(checkout), "cat-file", "--batch"],
        input=b"".join(revision + b"\n" for _, revision in expected.values()),
    )
    offset = 0
    for name, (mode, _) in expected.items():
        header_end = contents.index(b"\n", offset)
        size = int(contents[offset:header_end].split()[-1])
        start = header_end + 1
        if (
            _source_file_mode(files[name]) != (0o755 if mode == b"100755" else 0o644)
            or files[name].read_bytes() != contents[start:start + size]
        ):
            return False
        offset = start + size + 1
    return True


def _snapshot_dependencies(snapshot: Path | None) -> dict[str, dict[str, Any]]:
    """Read fixed-dependency provenance when a captured input set provides it."""
    metadata = snapshot / _SOURCE_METADATA if snapshot is not None else None
    if metadata is None or not metadata.is_file():
        return {}
    return json.loads(metadata.read_text()).get("dependencies", {})


def _unmaterialized_submodule(path: Path) -> bool:
    """Distinguish an absent checkout from local files that must not be replaced."""
    return not path.is_symlink() and (not path.exists() or path.is_dir() and not any(path.iterdir()))


def _git_dependency_inputs(
    root: Path, prefix: str, reference: Path | None = None,
    recorded_revisions: dict[str, str] | None = None,
) -> tuple[dict[str, Path], dict[str, str], bool]:
    """Read live dependency files, borrowing only absent submodules at recorded gitlinks."""
    checkout = root / prefix
    entries = subprocess.check_output(["git", "-C", str(checkout), "ls-files", "--stage", "-z"])
    links = {}
    for entry in entries.split(b"\0"):
        if entry:
            metadata, name = entry.split(b"\t", 1)
            mode, revision, _ = metadata.split()
            if mode == b"160000":
                links[os.fsdecode(name)] = revision.decode()
    files, revisions = {}, {}
    complete = True
    for relative in _git_names(checkout):
        name = prefix + "/" + relative
        path = root / name
        if relative not in links:
            if path.is_file():
                files[name] = path
            continue
        revisions[name] = links[relative]
        if (path / ".git").exists():
            nested, nested_revisions, ready = _git_dependency_inputs(root, name, reference, recorded_revisions)
            files.update(nested)
            revisions.update(nested_revisions)
            actual_revision = subprocess.check_output(
                ["git", "-C", str(path), "rev-parse", "HEAD"], text=True,
            ).strip()
            complete = complete and ready and actual_revision == links[relative]
        elif (
            reference is not None and _unmaterialized_submodule(path)
            and (recorded_revisions or {}).get(name) == links[relative]
        ):
            files.update({
                str(item.relative_to(reference)): item
                for item in (reference / name).rglob("*") if item.is_file()
            })
            revisions.update({
                nested: revision for nested, revision in (recorded_revisions or {}).items()
                if nested.startswith(name + "/")
            })
        else:
            if not _unmaterialized_submodule(path):
                raise DeploymentError(f"uninitialized dependency contains local files: {name}")
            complete = False
    return files, revisions, complete


def mooncake_inputs(
    root: Path, previous: Path | None, version: str,
) -> tuple[dict[str, Path], dict[str, Any]]:
    """Resolve fixed Mooncake inputs for capture or the installer's preparation decision."""
    prefix = "third_party/mooncake"
    preparer = root / "deploy/mooncake/prepare-source"
    environment = {**os.environ, "MOONCAKE_VERSION": version}
    described = subprocess.run(
        [str(preparer), "--describe"], cwd=root, env=environment,
        capture_output=True, text=True, check=False,
    )
    if described.returncode:
        raise DeploymentError(described.stderr.strip() or described.stdout.strip())
    revision, *patches = described.stdout.splitlines()
    previous_record = _snapshot_dependencies(previous).get(prefix, {})
    reusable = (
        previous is not None and previous_record.get("prepared") is True
        and previous_record.get("revision") == revision
        and previous_record.get("patches") == patches
        and all(
            (previous / name).is_file() and _same_source_file(root / name, previous / name)
            for name in ["deploy/mooncake/prepare-source", *patches]
        )
    )
    checkout = root / prefix
    if not (checkout / ".git").exists():
        if not _unmaterialized_submodule(checkout):
            raise DeploymentError(f"uninitialized dependency contains local files: {prefix}")
        if reusable:
            return {
                str(path.relative_to(previous)): path
                for path in (previous / prefix).rglob("*") if path.is_file()
            }, previous_record
        return {}, {"revision": revision, "patches": patches, "submodules": {}, "prepared": False}
    files, submodules, complete = _git_dependency_inputs(
        root, prefix, previous if reusable else None, previous_record.get("submodules", {}),
    )
    checked = subprocess.run(
        [str(preparer), "--check"], cwd=root, env=environment,
        capture_output=True, text=True, check=False,
    )
    if checked.returncode not in (0, 1):
        raise DeploymentError(checked.stderr.strip() or checked.stdout.strip())
    return files, {
        "revision": revision, "patches": patches, "submodules": submodules,
        "prepared": complete and checked.returncode == 0,
    }


def _source_file_mode(path: Path) -> int:
    """Use Git's executable-file distinction for private build snapshots."""
    return 0o755 if path.stat().st_mode & stat.S_IXUSR else 0o644


def _same_source_file(left: Path, right: Path) -> bool:
    """Compare build bytes and executable state independently of workstation metadata."""
    # filecmp caches deep comparisons by mtime; restored timestamps must not hide edits.
    filecmp.clear_cache()
    return _source_file_mode(left) == _source_file_mode(right) and filecmp.cmp(left, right, shallow=False)


def _checkout_source(root: Path) -> dict[str, Any] | None:
    """Record Git provenance without retaining local paths or remote credentials."""
    result = subprocess.run(["git", "-C", str(root), "rev-parse", "--verify", "HEAD"], capture_output=True, text=True, check=False)
    if result.returncode:
        return None
    changed = subprocess.check_output(["git", "-C", str(root), "status", "--porcelain", "--untracked-files=all"], text=True)
    return {"commit": result.stdout.strip(), "dirty": bool(changed)}


def build_sources(snapshot: Path, component: str) -> dict[str, Any] | None:
    """Read public provenance for a publication, including the model server's bound engines."""
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
