# SPDX-License-Identifier: Apache-2.0
# SPDX-FileCopyrightText: Copyright contributors to the Foretoken project

"""MACA peer-access discovery without changing the compute Worker's visibility."""

from __future__ import annotations

import ctypes
import errno
import json
import os
import subprocess
import sys
from pathlib import Path


def _accessible_devices() -> set[str]:
    """Inspect granted render devices; device-cgroup permission must allow opening each one."""
    devices = set()
    for path in Path("/dev/dri").glob("renderD*"):
        try:
            fd = os.open(path, os.O_RDWR | os.O_CLOEXEC)
        except OSError as error:
            if error.errno in (errno.EPERM, errno.EACCES, errno.ENOENT):
                continue
            # The MACA render driver can reject ordinary opens after permission
            # checks. Its runtime below owns the actual device/peer capability query.
            if error.errno != errno.EINVAL:
                raise
        else:
            os.close(fd)
        devices.add(
            (Path("/sys/class/drm") / path.name / "device")
            .resolve(strict=True)
            .name.lower()
        )
    return devices


def peer_access(device) -> dict | None:
    """Return this Worker's same-host peer capability, or None when no granted pair supports P2P."""
    import torch

    if len(_accessible_devices()) < 2:
        return None
    props = torch.cuda.get_device_properties(device)
    device_id = (
        f"{props.pci_domain_id:04x}:{props.pci_bus_id:02x}:{props.pci_device_id:02x}.0"
    )
    # The runtime caches visibility on initialization. A separate metadata-only
    # process can query granted peers without rebinding the live compute Worker.
    environment = dict(os.environ)
    environment.pop("CUDA_VISIBLE_DEVICES", None)
    environment.pop("MACA_VISIBLE_DEVICES", None)
    result = subprocess.run(
        [sys.executable, "-m", "foretoken_dt.maca"],
        env=environment,
        check=True,
        capture_output=True,
        text=True,
    )
    peers = json.loads(result.stdout)[device_id]
    if not peers:
        return None
    return {
        "host_id": Path("/proc/sys/kernel/random/boot_id").read_text().strip(),
        "device_id": device_id,
        "peer_devices": peers,
    }


def allocation_range(address: int) -> tuple[int, int]:
    """Resolve retained tensor storage to the allocation registered by Mooncake MACA IPC."""
    runtime = ctypes.CDLL("libmcruntime.so")
    query = runtime.mcMemGetAddressRange
    query.argtypes = [
        ctypes.POINTER(ctypes.c_uint64),
        ctypes.POINTER(ctypes.c_size_t),
        ctypes.c_uint64,
    ]
    base = ctypes.c_uint64()
    size = ctypes.c_size_t()
    code = query(ctypes.byref(base), ctypes.byref(size), address)
    if code != 0:
        raise RuntimeError(f"MACA allocation-range query failed: {code}")
    return base.value, size.value


def _discover() -> dict[str, list[str]]:
    """Query the pinned MACA runtime's peer matrix, restricted to granted render devices."""
    runtime = ctypes.CDLL("libmcruntime.so")
    count_devices = runtime.mcGetDeviceCount
    count_devices.argtypes = [ctypes.POINTER(ctypes.c_int)]
    pci_id = runtime.mcDeviceGetPCIBusId
    pci_id.argtypes = [ctypes.POINTER(ctypes.c_char), ctypes.c_int, ctypes.c_int]
    can_access = runtime.mcDeviceCanAccessPeer
    can_access.argtypes = [ctypes.POINTER(ctypes.c_int), ctypes.c_int, ctypes.c_int]

    def check(code):
        """Surface a broken runtime query rather than publishing invented capability."""
        if code != 0:
            raise RuntimeError(f"MACA peer-access query failed: {code}")

    count = ctypes.c_int()
    check(count_devices(ctypes.byref(count)))
    accessible = _accessible_devices()
    devices = {}
    for index in range(count.value):
        value = ctypes.create_string_buffer(64)
        check(pci_id(value, len(value), index))
        name = value.value.decode().lower()
        if name in accessible:
            devices[index] = name
    peers = {name: [] for name in devices.values()}
    for source, source_id in devices.items():
        for target, target_id in devices.items():
            if source == target:
                continue
            allowed = ctypes.c_int()
            check(can_access(ctypes.byref(allowed), source, target))
            if allowed.value:
                peers[source_id].append(target_id)
    return peers


if __name__ == "__main__":
    print(json.dumps(_discover()))
