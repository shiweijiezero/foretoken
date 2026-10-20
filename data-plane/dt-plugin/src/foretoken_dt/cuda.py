# SPDX-License-Identifier: Apache-2.0
# SPDX-FileCopyrightText: Copyright contributors to the Foretoken project

"""CUDA peer discovery and allocation identity for the Mooncake IPC adapter."""

from __future__ import annotations

import ctypes
import errno
import json
import os
import subprocess
import sys
from pathlib import Path


def _check(code: int) -> None:
    """Expose CUDA driver failures at the owning discovery or registration operation."""
    if code != 0:
        raise RuntimeError(f"CUDA IPC driver operation failed: {code}")


def _pci_id(driver, device: int) -> str:
    """Identify a CUDA device independently of a process's visible ordinal."""
    value = ctypes.create_string_buffer(32)
    _check(driver.cuDeviceGetPCIBusId(value, len(value), device))
    domain, bus, slot = value.value.decode().lower().split(":")
    return f"{int(domain, 16):04x}:{bus}:{slot}"


def peer_access(device) -> dict | None:
    """Report granted CUDA peers without changing the compute Worker's visibility."""
    import torch

    driver = ctypes.CDLL("libcuda.so.1")
    _check(driver.cuInit(0))
    index = device.index if device.index is not None else torch.cuda.current_device()
    device_id = _pci_id(driver, index)
    environment = dict(os.environ)
    environment.pop("CUDA_VISIBLE_DEVICES", None)
    result = subprocess.run(
        [sys.executable, "-m", "foretoken_dt.cuda"],
        env=environment,
        check=True,
        capture_output=True,
        text=True,
    )
    peers = json.loads(result.stdout).get(device_id, [])
    if not peers:
        return None
    return {
        "host_id": Path("/proc/sys/kernel/random/boot_id").read_text().strip(),
        "device_id": device_id,
        "peer_devices": peers,
    }


def allocation_range(address: int) -> tuple[int, int]:
    """Resolve Torch suballocations to the CUDA allocation shared by an IPC handle."""
    driver = ctypes.CDLL("libcuda.so.1")
    query = driver.cuMemGetAddressRange_v2
    query.argtypes = [
        ctypes.POINTER(ctypes.c_uint64),
        ctypes.POINTER(ctypes.c_size_t),
        ctypes.c_uint64,
    ]
    base = ctypes.c_uint64()
    size = ctypes.c_size_t()
    _check(query(ctypes.byref(base), ctypes.byref(size), address))
    return base.value, size.value


def _discover() -> dict[str, list[str]]:
    """Query CUDA's peer matrix using only GPU devices granted to this process."""
    granted = set()
    for info in Path("/proc/driver/nvidia/gpus").glob("*/information"):
        fields = dict(
            line.split(":", 1) for line in info.read_text().splitlines() if ":" in line
        )
        minor = int(fields["Device Minor"].strip())
        try:
            descriptor = os.open(f"/dev/nvidia{minor}", os.O_RDWR | os.O_CLOEXEC)
        except OSError as error:
            if error.errno not in (errno.EPERM, errno.EACCES, errno.ENOENT):
                raise
        else:
            os.close(descriptor)
            granted.add(info.parent.name.lower())
    if len(granted) < 2:
        return {}
    driver = ctypes.CDLL("libcuda.so.1")
    _check(driver.cuInit(0))
    count = ctypes.c_int()
    _check(driver.cuDeviceGetCount(ctypes.byref(count)))
    devices = {}
    for index in range(count.value):
        name = _pci_id(driver, index)
        if name in granted:
            devices[index] = name
    peers = {name: [] for name in devices.values()}
    for source, source_id in devices.items():
        for target, target_id in devices.items():
            if source == target:
                continue
            allowed = ctypes.c_int()
            _check(driver.cuDeviceCanAccessPeer(ctypes.byref(allowed), source, target))
            if allowed.value:
                peers[source_id].append(target_id)
    return peers


if __name__ == "__main__":
    print(json.dumps(_discover()))
