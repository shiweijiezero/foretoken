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
    previous_image: str | None = None,
    model_preparation_image: str | None = None,
    source_archive: Path | None = None,
    archive_url: str = "",
) -> Path:
    """Publish complete files and source evidence, checking the selected base before partial reuse."""
    prefix = _RELEASE_STAGING_PREFIX if release_owned else f".{binding}.staging-"
    if not release_owned:
        _clear_source_staging(destination.parent, binding)
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
            and metadata.get("modelPreparationImage") == model_preparation_image
        ):
            previous_files = metadata["files"]
    inputs = {path.relative_to(source): path for path in source.rglob("*")}
    if parts is not None:
        if metadata is None:
            raise FileNotFoundError(f"completed application is missing: {previous}")
        # A dependency-stage producer may retain the same native runtime while
        # replacing engine Python dependencies. It selects the compatible prior
        # application explicitly; ordinary file updates keep their current image.
        if metadata.get("image") != (previous_image if previous_image is not None else image):
            raise ValueError("partial publication requires the selected previous environment image")
        # Exports may include other build outputs. Accept only selected groups,
        # replacing each in full so deleted files cannot survive from the base.
        inputs = {
            relative: path for relative, path in inputs.items()
            if _application_part(relative, destination.parent.name) in parts
        }
        for entry in metadata["files"]:
            relative = Path(entry["path"])
            if not relative.parts or relative.is_absolute() or ".." in relative.parts:
                raise ValueError("application manifest contains an invalid file path")
            if _application_part(relative, destination.parent.name) not in parts:
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
        if sources is not None and source_archive is not None:
            # Evidence is a formal result, outside the component directories retired
            # by runtime GC. Reusing a completed application creates no new archive.
            application_parts = {
                _application_part(Path(entry["path"]), destination.parent.name) for entry in files
            }
            if parts is None or application_parts.intersection(parts):
                archive = (
                    destination.parent.parent
                    / "sources"
                    / destination.parent.name
                    / (destination.name + ".tar.gz")
                )
                if not archive.is_file():
                    archive.parent.mkdir(parents=True, exist_ok=True)
                    pending_archive = Path(temporary) / "inputs.tar.gz"
                    shutil.copyfile(source_archive, pending_archive)
                    pending_archive.chmod(0o444)
                    pending_archive.rename(archive)
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
        # Publish one transfer object alongside the file view used for incremental
        # assembly. Consumers extract only manifest-listed regular files.
        archive_name = "application.tar.gz"
        with tarfile.open(staging / archive_name, "w:gz", compresslevel=1, dereference=True) as package:
            for entry in files:
                package.add(staging / entry["path"], arcname=entry["path"], recursive=False)
        (staging / archive_name).chmod(0o444)
        (staging / "manifest.json").write_text(
            json.dumps(
                {
                    "binding": binding, "releaseOwned": release_owned,
                    "files": files, "archive": archive_name, "sources": sources,
                    **({"image": image} if image is not None else {}),
                    **({"modelPreparationImage": model_preparation_image} if model_preparation_image is not None else {}),
                }
            )
            + "\n"
        )
        if sources is not None and source_archive is not None:
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
) -> set[str]:
    """Remove unreferenced publications owned by this publisher and return its retained versions."""
    retained = set()
    for version in directory.iterdir():
        manifest = version / "manifest.json"
        if not manifest.is_file():
            continue
        metadata = json.loads(manifest.read_text())
        owned = (
            metadata.get("releaseOwned") is True
            if release_owned
            else metadata.get("binding") == binding
        )
        if owned:
            if version.name in keep:
                retained.add(version.name)
            else:
                shutil.rmtree(version)
    return retained


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


def publish_build(archive: Path, record: dict, destination: Path, binding: str, archive_url: str) -> None:
    """Publish one completed build and its exact input archive as an immutable development record."""
    if (destination / "build.json").is_file():
        return
    origin = destination.parents[2]
    for component, application in record["build"]["applications"].items():
        if not (origin / component / application["revision"] / "manifest.json").is_file():
            raise FileNotFoundError(f"completed {component} application is unavailable")
    destination.parent.mkdir(parents=True, exist_ok=True)
    _clear_source_staging(destination.parent, binding)
    with tempfile.TemporaryDirectory(prefix=f".{binding}.staging-", dir=destination.parent) as temporary:
        staging = Path(temporary) / "build"
        staging.mkdir()
        shutil.copyfile(archive, staging / "inputs.tar.gz")
        (staging / "inputs.tar.gz").chmod(0o444)
        (staging / "build.json").write_text(json.dumps({**record, "archive": archive_url}) + "\n")
        (staging / "build.json").chmod(0o444)
        staging.rename(destination)


def publish_batch(selection: dict) -> dict[str, str]:
    """Publish a source cohort and commit its build record only after every application succeeds."""
    binding = selection["binding"]
    archive = Path(selection["source_archive"])
    retained = selection["keep"]
    revisions = {}
    # Keep completed components reusable after a failure, without committing the
    # build record or leaving sibling writers running beyond the original error.
    for application in selection["applications"]:
        destination = publish(
            Path(application["source"]), Path(application["destination"]), binding,
            Path(application["previous"]) if application["previous"] else None,
            reuse_previous=True, sources=application["sources"], image=application["image"],
            parts=application["parts"], previous_image=application["previous_image"],
            model_preparation_image=application.get("model_preparation_image"),
            source_archive=archive, archive_url=application["archive_url"],
        )
        if retained is not None:
            retire(destination.parent, binding, set(retained) | {destination.name})
        revisions[application["component"]] = destination.name
    if completed := selection["build"]:
        record = json.loads(Path(completed["descriptor"]).read_text())
        record["build"]["applications"] = {
            component: {"revision": revision} for component, revision in revisions.items()
        }
        publish_build(
            archive, record, Path(completed["destination"]), binding,
            completed["url"] + "/inputs.tar.gz",
        )
    return revisions


def archive_inputs(workspace: Path, destination: Path) -> None:
    """Package exact synchronized inputs as their compiler owner for the publisher's read-only handoff."""
    inputs = json.loads((workspace.parent / "inputs.json").read_text())
    # The init container owns this temporary volume; failure discards it with the Job.
    # Include only synchronized inputs, retaining their original bytes and modes.
    with tarfile.open(destination, "w:gz", compresslevel=3, dereference=True) as package:
        for name in sorted(inputs):
            package.add(workspace / name, arcname=name, recursive=False)
    destination.chmod(0o444)


def _application_part(path: Path, component: str) -> str:
    """Assign each application file to the build output that replaces it as a group."""
    if component == "control-plane":
        return "go"
    if path.parts[0] == "python":
        return "python"
    if path.parts[0] in {"engine", "engine-environment.json"}:
        return "engine"
    return "rust"


def _clear_source_staging(directory: Path, binding: str) -> None:
    """Remove one stopped publisher's staging files before reuse or retirement."""
    for abandoned in directory.glob(f".{binding}.staging-*"):
        shutil.rmtree(abandoned)


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
    elif sys.argv[1] == "--archive-inputs":
        archive_inputs(Path(sys.argv[2]), Path(sys.argv[3]))
    elif sys.argv[1] == "--batch":
        print(json.dumps(publish_batch(json.loads(Path(sys.argv[2]).read_text()))))
