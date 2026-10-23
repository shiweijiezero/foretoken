# SPDX-License-Identifier: Apache-2.0
# SPDX-FileCopyrightText: Copyright contributors to the Foretoken project

"""Prepare platform command settings and scope their subprocess lifetimes."""

from __future__ import annotations

import os

from foretoken.arguments import InstallCommand, UninstallCommand
from foretoken.kubernetes.client import timeout_seconds
from foretoken.platform.lifecycle import PlatformLifecycle
from foretoken.processes.execution import CommandExecution


def install(command: InstallCommand) -> None:
    """Resolve the CLI registry selection and install the platform within its command scope."""
    with CommandExecution(timeout_seconds(command.timeout)):
        registry = command.oci_registry or os.environ.get("FORETOKEN_OCI_REGISTRY")
        PlatformLifecycle(registry).install(command)


def uninstall(command: UninstallCommand) -> None:
    """Remove the CLI-managed platform while retaining command cancellation and cleanup ownership."""
    with CommandExecution(timeout_seconds(command.timeout)):
        PlatformLifecycle().uninstall(command)
