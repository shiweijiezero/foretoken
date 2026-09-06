# SPDX-License-Identifier: Apache-2.0
# SPDX-FileCopyrightText: Copyright contributors to the Foretoken project

"""Foretoken command-line interface."""

from importlib.metadata import version

from packaging.version import Version


def package_version() -> str:
    """Return the installed Foretoken distribution version."""
    return version("foretoken")


def platform_version() -> str:
    """Return the SemVer platform version corresponding to the Python package."""
    parsed = Version(package_version())
    base = ".".join(str(part) for part in parsed.release)
    if parsed.pre is not None:
        stage, serial = parsed.pre
        stage_name = {"a": "alpha", "b": "beta", "rc": "rc"}[stage]
        return f"{base}-{stage_name}.{serial}"
    if parsed.dev is not None:
        return f"{base}-dev.{parsed.dev}"
    return base
