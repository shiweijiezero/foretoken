# SPDX-License-Identifier: Apache-2.0
# SPDX-FileCopyrightText: Copyright contributors to the Foretoken project

"""顶层 benchmark 命令入口；当前只分发已实现的 HTTP 性能评测。"""

from benchmarks.performance.main import main

__all__ = ["main"]


if __name__ == "__main__":
    main()
