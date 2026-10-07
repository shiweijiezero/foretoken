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


def publish(
    source: Path,
    destination: Path,
    binding: str,
    previous: Path | None,
    *,
    release_owned: bool = False,
) -> None:
    """Expose an immutable directory, sharing unchanged files with its previous version."""
    if (destination / "manifest.json").is_file():
        return
    if not source.is_dir():
        raise FileNotFoundError(f"application export is missing: {source}")
    destination.parent.mkdir(parents=True, exist_ok=True)
    prefix = _RELEASE_STAGING_PREFIX if release_owned else f".{binding}.staging-"
    if not release_owned:
        for abandoned in destination.parent.glob(prefix + "*"):
            shutil.rmtree(abandoned)
    with tempfile.TemporaryDirectory(
        prefix=prefix, dir=destination.parent
    ) as temporary:
        staging = Path(temporary) / "payload"
        staging.mkdir()
        files = []
        for origin in sorted(source.rglob("*")):
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
            relative = origin.relative_to(source)
            target = staging / relative
            target.parent.mkdir(parents=True, exist_ok=True)
            mode = (stat.S_IMODE(origin.stat().st_mode) & 0o111) | 0o444
            old = previous / relative if previous is not None else None
            if (
                old is not None
                and old.is_file()
                and ((stat.S_IMODE(old.stat().st_mode) & 0o111) | 0o444) == mode
                and filecmp.cmp(origin, old, shallow=False)
            ):
                os.link(old, target)
            else:
                shutil.copyfile(origin, target)
                target.chmod(mode)
            files.append(
                {
                    "path": relative.as_posix(),
                    "mode": mode,
                    "size": target.stat().st_size,
                }
            )
        (staging / "manifest.json").write_text(
            json.dumps(
                {"binding": binding, "releaseOwned": release_owned, "files": files}
            )
            + "\n"
        )
        staging.rename(destination)


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
        publish(
            Path(sys.argv[1]),
            destination,
            binding,
            Path(sys.argv[4]) if sys.argv[4] else None,
        )
        retained = json.loads(sys.argv[5])
        if retained is not None:
            retire(destination.parent, binding, set(retained) | {destination.name})
