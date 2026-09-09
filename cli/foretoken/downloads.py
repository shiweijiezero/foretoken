# SPDX-License-Identifier: Apache-2.0
# SPDX-FileCopyrightText: Copyright contributors to the Foretoken project

"""Download locations for public, digest-pinned installation and build artifacts."""

from __future__ import annotations

import os
import re
import subprocess
import sys
from pathlib import Path
import urllib.error
import urllib.request
from functools import cached_property


class DownloadSources:
    """Select public artifact transport once per installation or source build."""

    @cached_property
    def docker_mirror(self) -> str:
        """Return the selected mirror prefix, or empty for the original registry."""
        configured = os.environ.get("FORETOKEN_IMAGE_MIRROR", "auto").strip().rstrip("/")
        if configured in {"", "off"}:
            return ""
        if configured != "auto":
            return configured
        try:
            with urllib.request.urlopen("https://registry-1.docker.io/v2/", timeout=5):
                return ""
        except urllib.error.HTTPError as error:
            if error.code < 500:
                return ""
        except (urllib.error.URLError, TimeoutError, ConnectionError):
            pass
        # Only public artifacts pinned by their upstream OCI manifest digest use this path.
        # Docker/containerd and Helm validate the downloaded content against that digest.
        return "m.daocloud.io"

    def public_image(self, reference: str) -> str:
        """Select transport for a caller-owned public image without changing its OCI digest."""
        if not reference.startswith("docker.io/") or "@sha256:" not in reference:
            return reference
        mirror = self.docker_mirror
        return f"{mirror}/{reference}" if mirror else reference

    def public_chart(self, reference: str) -> str:
        """Select transport for a public OCI chart pinned by the chart adapter."""
        if not reference.startswith("oci://"):
            return reference
        return "oci://" + self.public_image(reference.removeprefix("oci://"))


def build_image(args: list[str]) -> int:
    """Build a repository Dockerfile with source-owned public image defaults and explicit overrides."""
    dockerfile = Path(args[args.index("-f") + 1])
    supplied = {
        args[index + 1].split("=", 1)[0]
        for index, arg in enumerate(args[:-1]) if arg == "--build-arg"
    }
    sources = DownloadSources()
    overrides: list[str] = []
    for name, default in re.findall(r"^ARG ([A-Z_]+)(?:=(.*))?$", dockerfile.read_text(), re.MULTILINE):
        if name in supplied:
            continue
        if name in os.environ:
            overrides.extend(["--build-arg", name])
        elif name.endswith("_IMAGE") and default.startswith("docker.io/"):
            reference = sources.public_image(default)
            if reference != default:
                print(f"Build image: {reference}", flush=True)
                overrides.extend(["--build-arg", f"{name}={reference}"])
    return subprocess.run(["docker", "build", *overrides, *args], check=False).returncode


if __name__ == "__main__":
    raise SystemExit(build_image(sys.argv[1:]))
