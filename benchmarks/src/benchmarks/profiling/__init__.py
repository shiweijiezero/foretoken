# SPDX-License-Identifier: Apache-2.0
# SPDX-FileCopyrightText: Copyright contributors to the Foretoken project

"""Coordinate benchmark requests with the existing service-owned capture lifecycle."""

from foretoken.manifest import DeploymentError


class CaptureCleanupError(DeploymentError):
    """Tell benchmark deployment cleanup to retain runtimes whose stop is unconfirmed."""
