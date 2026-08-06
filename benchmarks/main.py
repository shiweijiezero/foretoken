# SPDX-License-Identifier: Apache-2.0
# SPDX-FileCopyrightText: Copyright contributors to the Foretoken project


"""CLI entry point: parse arguments and run the selected benchmark."""

from __future__ import annotations

import asyncio
import logging
from typing import Sequence

from benchmarks.arguments import parse_arguments
from benchmarks.runner.select_runner import select_runner

logger = logging.getLogger(__name__)


def main(argv: Sequence[str] | None = None) -> None:
    """Parse CLI, log config summary, and run the selected benchmark."""
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s [%(name)s] %(message)s",
    )
    config = parse_arguments(argv)
    logger.info("%s", config.summary())
    asyncio.run(select_runner(config).run())


if __name__ == "__main__":
    main()
