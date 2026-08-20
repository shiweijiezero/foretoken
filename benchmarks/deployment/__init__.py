# SPDX-License-Identifier: Apache-2.0
# SPDX-FileCopyrightText: Copyright contributors to the Foretoken project


"""Discover benchmark endpoints from deployed Foretoken services."""

from benchmarks.deployment.discovery import discover_endpoint
from benchmarks.deployment.manifest import DeploymentError

__all__ = ["DeploymentError", "discover_endpoint"]
