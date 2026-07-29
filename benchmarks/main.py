# SPDX-License-Identifier: Apache-2.0
# SPDX-FileCopyrightText: Copyright contributors to the Foretoken project


"""Benchmark entry: parse → runner."""

from __future__ import annotations

import asyncio
from typing import Sequence

from benchmarks.arguments import parse_arguments
from benchmarks.runner.select import select_runner


def main(argv: Sequence[str] | None = None) -> None:
    """Parse CLI, print config summary, and run the selected benchmark."""
    config = parse_arguments(argv)
    print(config.summary())
    asyncio.run(select_runner(config).run())


if __name__ == "__main__":
    main()
