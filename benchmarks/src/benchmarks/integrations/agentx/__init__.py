# SPDX-License-Identifier: Apache-2.0
# SPDX-FileCopyrightText: Copyright contributors to the Foretoken project

"""Versioned command and report boundary for the official AgentX harness."""

HARNESS_VERSION = "1.0.12"


class AgentXExecutionError(RuntimeError):
    """The native client stopped before producing a completed profiling measurement."""
