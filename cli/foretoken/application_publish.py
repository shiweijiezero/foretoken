# SPDX-License-Identifier: Apache-2.0
# SPDX-FileCopyrightText: Copyright contributors to the Foretoken project

"""Publish completed build files on the platform's persistent application volume."""

from __future__ import annotations

import filecmp
import json
import os
import shutil
import stat
import sys
import tarfile
import tempfile
from pathlib import Path
from urllib.parse import urlsplit
from urllib.request import HTTPRedirectHandler, Request, build_opener

_RELEASE_STAGING_PREFIX = ".release.staging-"


class _ReleaseRedirectHandler(HTTPRedirectHandler):
    """Keep release credentials on the configured scheme and authority."""

    def redirect_request(self, req, fp, code, msg, headers, newurl):
        """Follow normal HTTP redirects without forwarding credentials to another source."""
        redirect = super().redirect_request(req, fp, code, msg, headers, newurl)
        if urlsplit(req.full_url)[:2] != urlsplit(newurl)[:2]:
            redirect.remove_header("Authorization")
        return redirect


def import_release(
    source: str,
    destination: Path,
    revision: str,
    binding: str,
    previous: dict[str, str],
    keep: set[str] | None,
) -> None:
    """Import or reuse release files while the caller holds the origin's unique release Job."""
    # Exclusive release ownership makes interrupted staging recoverable even on reuse.
    for component in ("control-plane", "frontend", "model-server"):
        for abandoned in (destination / component).glob(_RELEASE_STAGING_PREFIX + "*"):
            shutil.rmtree(abandoned)
    missing = [
        component
        for component in ("control-plane", "frontend", "model-server")
        if not (destination / component / revision / "manifest.json").is_file()
    ]
    if missing:
        request = Request(source)
        if credential := os.environ.get("FORETOKEN_RELEASE_AUTHORIZATION"):
            request.add_header("Authorization", credential)
        opener = build_opener(_ReleaseRedirectHandler())
        with tempfile.TemporaryDirectory(dir="/tmp") as temporary:
            archive = Path(temporary) / "applications.tar.gz"
            with opener.open(request) as response, archive.open("wb") as output:
                shutil.copyfileobj(response, output)
            payload = Path(temporary) / "payload"
            with tarfile.open(archive) as package:
                package.extractall(payload, filter="data")
            for component in missing:
                prior = previous.get(component)
                publish(
                    payload / component,
                    destination / component / revision,
                    binding,
                    destination / component / prior if prior else None,
                    release_owned=True,
                )
    if keep is not None:
        for component in ("control-plane", "frontend", "model-server"):
            retire(
                destination / component, binding, keep | {revision}, release_owned=True
            )


def _application_part(path: Path) -> str:
    """Assign each application file to the build output that replaces it as a group."""
    if path.parts[0] == "python":
        return "python"
    if path.parts[0] in {"engine", "engine-environment.json"}:
        return "engine"
    return "rust"


def archive_inputs(workspace: Path, destination: Path, binding: str) -> None:
    """Persist the synchronized build snapshot independently of compiler and runtime GC."""
    if destination.is_file():
        return
    inputs = json.loads((workspace.parent / "inputs.json").read_text())
    destination.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(
        prefix=f".{binding}.staging-", dir=destination.parent
    ) as temporary:
        archive = Path(temporary) / "inputs.tar.gz"
        # Only synchronized inputs belong to the evidence, not generated Dockerfiles
        # or compiler outputs that may also live in the builder's workspace.
        with tarfile.open(archive, "w:gz", dereference=True) as package:
            for name in sorted(inputs):
                package.add(workspace / name, arcname=name, recursive=False)
        archive.chmod(0o444)
        archive.rename(destination)


def publish(
    source: Path,
    destination: Path,
    binding: str,
    previous: Path | None,
    *,
    release_owned: bool = False,
    reuse_previous: bool = False,
    sources: dict | None = None,
    image: str | None = None,
    parts: list[str] | None = None,
    workspace: Path | None = None,
    archive_url: str = "",
) -> Path:
    """Publish a complete application, optionally replacing selected outputs of a completed base."""
    prefix = _RELEASE_STAGING_PREFIX if release_owned else f".{binding}.staging-"
    if not release_owned:
        # The caller stops this binding's previous publisher before starting another.
        for directory in (
            destination.parent,
            destination.parent.parent / "sources" / destination.parent.name,
        ):
            for abandoned in directory.glob(prefix + "*"):
                shutil.rmtree(abandoned)
    if (destination / "manifest.json").is_file():
        return destination
    if not source.is_dir():
        raise FileNotFoundError(f"application export is missing: {source}")
    previous_files = None
    metadata = None
    if previous is not None and (previous / "manifest.json").is_file():
        metadata = json.loads((previous / "manifest.json").read_text())
        if (
            reuse_previous
            and metadata.get("binding") == binding
            and not metadata.get("releaseOwned")
            and metadata.get("image") == image
        ):
            previous_files = metadata["files"]
    inputs = {path.relative_to(source): path for path in source.rglob("*")}
    if parts is not None:
        if metadata is None:
            raise FileNotFoundError(f"completed application is missing: {previous}")
        if metadata.get("image") != image:
            raise ValueError("partial publication requires the previous environment image")
        # Exports may include other build outputs. Accept only selected groups,
        # replacing each in full so deleted files cannot survive from the base.
        inputs = {
            relative: path for relative, path in inputs.items()
            if _application_part(relative) in parts
        }
        for entry in metadata["files"]:
            relative = Path(entry["path"])
            if not relative.parts or relative.is_absolute() or ".." in relative.parts:
                raise ValueError("application manifest contains an invalid file path")
            if _application_part(relative) not in parts:
                inputs[relative] = previous / relative
    destination.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(
        prefix=prefix, dir=destination.parent
    ) as temporary:
        staging = Path(temporary) / "payload"
        staging.mkdir()
        files = []
        unchanged = previous_files is not None
        for relative, origin in sorted(inputs.items()):
            if origin.is_symlink():
                raise ValueError(
                    f"application exports must not contain symlinks: {origin}"
                )
            if origin.is_dir():
                continue
            if not origin.is_file():
                raise ValueError(
                    f"application exports must contain regular files: {origin}"
                )
            target = staging / relative
            target.parent.mkdir(parents=True, exist_ok=True)
            mode = (stat.S_IMODE(origin.stat().st_mode) & 0o111) | 0o444
            old = previous / relative if previous is not None else None
            if (
                old is not None
                and old.is_file()
                and ((stat.S_IMODE(old.stat().st_mode) & 0o111) | 0o444) == mode
                and (origin == old or filecmp.cmp(origin, old, shallow=False))
            ):
                os.link(old, target)
            else:
                unchanged = False
                shutil.copyfile(origin, target)
                target.chmod(mode)
            files.append(
                {
                    "path": relative.as_posix(),
                    "mode": mode,
                    "size": target.stat().st_size,
                }
            )
        # Reuse requires the same environment, published bytes and executable modes.
        # Comparing the complete manifest also detects files removed from the new export.
        if previous is not None and unchanged and files == previous_files:
            return previous
        if sources is not None and workspace is not None:
            # Evidence is a formal result, outside the component directories retired
            # by runtime GC. Reusing a completed application creates no new archive.
            application_parts = {
                _application_part(Path(entry["path"])) for entry in files
            }
            if parts is None or application_parts.intersection(parts):
                archive = (
                    destination.parent.parent
                    / "sources"
                    / destination.parent.name
                    / (destination.name + ".tar.gz")
                )
                archive_inputs(workspace, archive, binding)
            sources = {**sources, "archive": archive_url}
            adopted = {}
            for part in sorted(application_parts):
                if parts is not None and part not in parts:
                    prior_sources = metadata.get("sources")
                    if prior_sources is None:
                        adopted[part] = None
                    elif "parts" in prior_sources:
                        adopted[part] = prior_sources["parts"][part]
                    else:
                        adopted[part] = {
                            key: value
                            for key, value in prior_sources.items()
                            if key != "engines" or part == "engine"
                        }
                else:
                    adopted[part] = {
                        key: value
                        for key, value in sources.items()
                        if key != "engines" or part == "engine"
                    }
            sources = {"parts": adopted}
        (staging / "manifest.json").write_text(
            json.dumps(
                {
                    "binding": binding, "releaseOwned": release_owned,
                    "files": files, "sources": sources,
                    **({"image": image} if image is not None else {}),
                }
            )
            + "\n"
        )
        if sources is not None and workspace is not None:
            # Keep the same immutable manifest inode as source evidence when
            # runtime GC removes its application-directory link.
            evidence = destination.parent.parent / "sources" / destination.parent.name / (destination.name + ".json")
            evidence.parent.mkdir(parents=True, exist_ok=True)
            pending_evidence = Path(temporary) / "source-manifest.json"
            os.link(staging / "manifest.json", pending_evidence)
            pending_evidence.replace(evidence)
        staging.rename(destination)
    return destination


def retire(
    directory: Path, binding: str, keep: set[str], *, release_owned: bool = False
) -> None:
    """Remove completed versions belonging to the release or the source publisher binding."""
    for version in directory.iterdir():
        manifest = version / "manifest.json"
        if version.name in keep or not manifest.is_file():
            continue
        metadata = json.loads(manifest.read_text())
        owned = (
            metadata.get("releaseOwned") is True
            if release_owned
            else metadata.get("binding") == binding
        )
        if owned:
            shutil.rmtree(version)


if __name__ == "__main__":
    if sys.argv[1] == "--release":
        selection = json.loads(Path(sys.argv[6]).read_text())
        retained = selection["keep"]
        import_release(
            sys.argv[2],
            Path(sys.argv[3]),
            sys.argv[4],
            sys.argv[5],
            selection["previous"],
            set(retained) if retained is not None else None,
        )
    else:
        destination = Path(sys.argv[2])
        binding = sys.argv[3]
        destination = publish(
            Path(sys.argv[1]),
            destination,
            binding,
            Path(sys.argv[4]) if sys.argv[4] else None,
            reuse_previous=json.loads(sys.argv[6]),
            sources=json.loads(sys.argv[7]),
            image=json.loads(sys.argv[8]),
            parts=json.loads(sys.argv[9]),
            workspace=Path(sys.argv[10]),
            archive_url=sys.argv[11],
        )
        retained = json.loads(sys.argv[5])
        if retained is not None:
            retire(destination.parent, binding, set(retained) | {destination.name})
        print(json.dumps({"revision": destination.name}))
