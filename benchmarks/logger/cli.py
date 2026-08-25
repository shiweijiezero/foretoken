# SPDX-License-Identifier: Apache-2.0
# SPDX-FileCopyrightText: Copyright contributors to the Foretoken project


"""Console logging and user-visible benchmark output."""

from __future__ import annotations

import logging


def configure_logging(console_enabled: bool) -> None:
    """Configure console logs without disrupting progress bars."""
    logging.basicConfig(
        level=logging.INFO if console_enabled else logging.WARNING,
        format="%(asctime)s %(levelname)s [%(name)s] %(message)s",
    )
    logging.getLogger("httpx").setLevel(logging.WARNING)
    logging.getLogger("httpcore").setLevel(logging.WARNING)


def print_endpoint(endpoint_url: str, models: tuple[str, ...], hostname: str) -> None:
    """Print the public endpoint selected for a deployment benchmark."""
    print(f"Endpoint: {endpoint_url}")
    if hostname:
        print(f"Hostname: {hostname}")
    print(f"Models: {', '.join(models)}")
