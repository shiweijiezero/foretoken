# SPDX-License-Identifier: Apache-2.0
# SPDX-FileCopyrightText: Copyright contributors to the Foretoken project

"""Foretoken command-line interface."""

from importlib.metadata import version

from packaging.version import Version


def package_version() -> str:
    """Return the installed Foretoken distribution version."""
    return version("foretoken")


def platform_version_for(distribution_version: str) -> str:
    """Return the SemVer platform version for a Foretoken distribution version."""
    parsed = Version(distribution_version)
    base = ".".join(str(part) for part in parsed.release)
    if parsed.pre is not None:
        stage, serial = parsed.pre
        stage_name = {"a": "alpha", "b": "beta", "rc": "rc"}[stage]
        return f"{base}-{stage_name}.{serial}"
    if parsed.dev is not None:
        return f"{base}-dev.{parsed.dev}"
    # Stable and post-release packages use the stable platform assets.
    return base


def platform_version() -> str:
    """Return the platform version corresponding to the installed package."""
    return platform_version_for(package_version())
