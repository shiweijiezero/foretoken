# SPDX-License-Identifier: Apache-2.0
# SPDX-FileCopyrightText: Copyright contributors to the Foretoken project

"""Top-level benchmark command entry point; currently dispatches the implemented HTTP benchmark."""

from benchmarks.performance.main import main

__all__ = ["main"]


if __name__ == "__main__":
    main()
