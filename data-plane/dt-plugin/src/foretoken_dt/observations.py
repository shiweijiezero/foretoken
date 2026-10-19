# SPDX-License-Identifier: Apache-2.0
# SPDX-FileCopyrightText: Copyright contributors to the Foretoken project

"""Local RPC wall-time accounting shared by transfer preparation and role endpoints."""

from dataclasses import dataclass


@dataclass
class RpcTiming:
    """Accumulate local Worker RPC phases; monotonic clocks share one host boot epoch."""

    calls: int = 0
    dispatch_seconds: float = 0.0
    worker_seconds: float = 0.0
    return_seconds: float = 0.0

    def record(self, sent, received, started, finished):
        """Account for a completed RPC, including queue/IPC delay before Worker entry."""
        self.calls += 1
        self.dispatch_seconds += started - sent
        self.worker_seconds += finished - started
        self.return_seconds += received - finished

    def to_wire(self):
        """Expose only measured aggregates to round observations and peer control replies."""
        return {
            "calls": self.calls,
            "dispatch_seconds": self.dispatch_seconds,
            "worker_seconds": self.worker_seconds,
            "return_seconds": self.return_seconds,
        }
