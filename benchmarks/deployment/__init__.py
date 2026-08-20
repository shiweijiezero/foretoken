# SPDX-License-Identifier: Apache-2.0
# SPDX-FileCopyrightText: Copyright contributors to the Foretoken project


"""Discover benchmark targets from deployed Foretoken services."""

from benchmarks.deployment.manifest import DeploymentError
from benchmarks.deployment.target import benchmark_deployment

__all__ = ["DeploymentError", "benchmark_deployment"]
