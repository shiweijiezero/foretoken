# SPDX-License-Identifier: Apache-2.0
# SPDX-FileCopyrightText: Copyright contributors to the Foretoken project

"""Foretoken command-line interface."""

from importlib.metadata import version


def package_version() -> str:
    """Return the installed Foretoken distribution version."""
    return version("foretoken")
