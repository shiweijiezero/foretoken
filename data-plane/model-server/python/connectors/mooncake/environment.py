# SPDX-License-Identifier: Apache-2.0
# SPDX-FileCopyrightText: Copyright contributors to the Foretoken project

"""Select Mooncake's native GPU registration path before loading its library."""

import ctypes
import os
from pathlib import Path

# Stable CUDA Driver API identifiers, independent of the installed toolkit headers.
_CUDA_ERROR_NO_DEVICE = 100
_CUDA_ERROR_INVALID_VALUE = 1
_CUDA_ERROR_NOT_SUPPORTED = 801
_CU_DEVICE_ATTRIBUTE_DMA_BUF_SUPPORTED = 124


def prefer_dma_buf() -> bool:
    """Tell model-server startup when CUDA DMA-BUF can replace unavailable peer memory.

    Preserve explicit configuration and loaded peer-memory drivers. The isolated
    startup process queries device attributes without creating CUDA contexts or buffers.
    """
    if "WITH_NVIDIA_PEERMEM" in os.environ or any(
        Path(f"/sys/module/{module}").is_dir()
        for module in ("nvidia_peermem", "nv_peer_mem")
    ):
        return False
    try:
        driver = ctypes.CDLL("libcuda.so.1")
    except OSError:
        # Non-CUDA runtimes retain their native registration implementation.
        return False

    integer = ctypes.c_int
    for name, arguments in (
        ("cuInit", (ctypes.c_uint,)),
        ("cuDeviceGetCount", (ctypes.POINTER(integer),)),
        ("cuDeviceGet", (ctypes.POINTER(integer), integer)),
        ("cuDeviceGetAttribute", (ctypes.POINTER(integer), integer, integer)),
    ):
        function = getattr(driver, name)
        function.argtypes = arguments
        function.restype = integer

    status = driver.cuInit(0)
    if status == _CUDA_ERROR_NO_DEVICE:
        return False
    if status != 0:
        raise RuntimeError(
            f"CUDA initialization for DMA-BUF discovery failed: {status}"
        )
    count = integer()
    status = driver.cuDeviceGetCount(ctypes.byref(count))
    if status != 0:
        raise RuntimeError(f"CUDA device discovery failed: {status}")
    if count.value == 0:
        return False
    for ordinal in range(count.value):
        device = integer()
        status = driver.cuDeviceGet(ctypes.byref(device), ordinal)
        if status != 0:
            raise RuntimeError(f"CUDA device {ordinal} lookup failed: {status}")
        supported = integer()
        status = driver.cuDeviceGetAttribute(
            ctypes.byref(supported),
            _CU_DEVICE_ATTRIBUTE_DMA_BUF_SUPPORTED,
            device.value,
        )
        if status in (_CUDA_ERROR_INVALID_VALUE, _CUDA_ERROR_NOT_SUPPORTED):
            return False
        if status != 0:
            raise RuntimeError(
                f"CUDA device {ordinal} DMA-BUF discovery failed: {status}"
            )
        if not supported.value:
            return False
    return True
