# SPDX-License-Identifier: Apache-2.0
# SPDX-FileCopyrightText: Copyright contributors to the Foretoken project

"""Discover external command-line tools used by Foretoken."""

from __future__ import annotations

import os
import shutil
from pathlib import Path

from foretoken.manifest import DeploymentError

_TOOL_PATHS = {
    "kubectl": (
        "/var/lib/rancher/rke2/bin/kubectl",
        "/var/lib/rancher/k3s/data/current/bin/kubectl",
    ),
    "helm": (
        "/var/lib/rancher/rke2/bin/helm",
        "/var/lib/rancher/k3s/data/current/bin/helm",
    ),
}


def resolve_tool(name: str, environment: str) -> str:
    """Return an executable tool from an override, PATH, or known cluster paths."""
    override = os.environ.get(environment)
    if override:
        resolved = shutil.which(override) if "/" not in override else override
        if resolved and os.access(resolved, os.X_OK):
            return resolved
        raise DeploymentError(f"{environment} points to a non-executable tool: {override}")

    resolved = shutil.which(name)
    if resolved:
        return resolved

    for candidate in _TOOL_PATHS.get(name, ()):
        if Path(candidate).is_file() and os.access(candidate, os.X_OK):
            return candidate

    candidates = ", ".join(_TOOL_PATHS.get(name, ()))
    raise DeploymentError(
        f"{name} is required; install it, add it to PATH, or set {environment}"
        + (f" (also checked: {candidates})" if candidates else "")
    )
