# SPDX-License-Identifier: Apache-2.0
# SPDX-FileCopyrightText: Copyright contributors to the Foretoken project

"""Shared value types for platform lifecycle adapters."""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class ReleaseRef:
    """A named Helm release whose lifecycle may be owned by the CLI."""

    name: str
    namespace: str

    @property
    def display_name(self) -> str:
        """Return the stable release identity shown in install plans."""
        return f"{self.namespace}/{self.name}"


@dataclass(frozen=True)
class PlatformGatewayConfig:
    """Effective Gateway mode stored in one platform Helm release."""

    mode: str
    create: bool
    controller_name: str
    name: str
    namespace: str
    section_name: str
