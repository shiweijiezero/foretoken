# SPDX-License-Identifier: Apache-2.0
# SPDX-FileCopyrightText: Copyright contributors to the Foretoken project

"""Dispatch calibration work, then replace the launcher with the original engine."""

import importlib.util
import json
import os
import sys


def main() -> None:
    """Run a calibration probe or exec the original engine in its existing managed process group."""
    mode = sys.argv[1]
    options = json.loads(sys.argv[2])
    if mode == "probe":
        from .probe import run_probe

        run_probe(options)
        return
    if importlib.util.find_spec("vllm_metax") is not None:
        from vllm.platforms import current_platform

        if current_platform.device_name == "maca":
            from vllm_metax.utils import mccl

            if hasattr(mccl, "configure_mccl_visible_hcas"):
                from .calibration import McclNetworkCalibration

                McclNetworkCalibration(options).run()
    os.execv(sys.executable, [sys.executable, *sys.argv[3:]])


if __name__ == "__main__":
    main()
