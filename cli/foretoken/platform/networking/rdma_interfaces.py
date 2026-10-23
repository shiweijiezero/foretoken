# SPDX-License-Identifier: Apache-2.0
# SPDX-FileCopyrightText: Copyright contributors to the Foretoken project

"""Report node-visible RoCE parents for the platform installer's short-lived Pod."""

import errno
import ipaddress
import json
import socket
import struct
from pathlib import Path

# Linux netdevice queries; these do not change interface configuration.
_SIOCGIFADDR = 0x8915
_SIOCGIFNETMASK = 0x891B


def is_roce_port(port: Path) -> bool:
    """Identify InfiniBand-transport Ethernet ports for node setup and inspection."""
    # IB_CA (1) excludes iWARP; a GID table distinguishes RoCE from raw-packet-only ports.
    return (
        (port.parent.parent / "node_type").read_text().startswith("1:")
        and (port / "link_layer").read_text().strip() == "Ethernet"
        and (port / "gids/0").is_file()
    )


def discover_interfaces() -> dict:
    """Return installer-visible RoCE parents without opening accelerator or verbs devices."""
    import fcntl

    net = Path("/sys/class/net")
    masters = {
        interface.name
        for interface in net.iterdir()
        if (interface / "ifalias").read_text().strip().startswith("foretoken-rdma:")
    }
    ports = tuple(Path("/sys/class/infiniband").glob("*/ports/*"))
    active_roce_ports = 0
    for port in ports:
        if not is_roce_port(port) or not (port / "state").read_text().startswith("4:"):
            continue
        active_roce_ports += 1
        for entry in (port / "gid_attrs/ndevs").iterdir():
            try:
                name = entry.read_text().strip()
            except OSError as error:
                if error.errno in (errno.EINVAL, errno.ENOENT):
                    continue
                raise
            if name and (net / name).exists():
                masters.add(name)

    # Dormant node anchors keep their names without becoming active Pod attachments.
    connected = set()
    for name in masters:
        try:
            if (net / name / "carrier").read_text().strip() == "1":
                connected.add(name)
        except OSError as error:
            if error.errno != errno.EINVAL:
                raise
    masters = connected
    prefixes = {name: [] for name in masters}
    with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as connection:
        for name in masters:
            request = struct.pack("256s", name.encode())
            try:
                address = fcntl.ioctl(connection, _SIOCGIFADDR, request)[20:24]
                mask = fcntl.ioctl(connection, _SIOCGIFNETMASK, request)[20:24]
            except OSError as error:
                if error.errno in (errno.EADDRNOTAVAIL, errno.ENODEV):
                    continue
                raise
            length = int.from_bytes(mask, "big").bit_count()
            prefixes[name].append(
                str(
                    ipaddress.ip_network(
                        f"{socket.inet_ntoa(address)}/{length}", strict=False
                    )
                )
            )
    ipv6 = Path("/proc/net/if_inet6")
    if ipv6.exists():
        for line in ipv6.read_text().splitlines():
            address, _, length, scope, _, name = line.split()
            if name in masters and int(scope, 16) == 0:
                prefixes[name].append(
                    str(
                        ipaddress.IPv6Network(
                            (int(address, 16), int(length, 16)), strict=False
                        )
                    )
                )
    return {
        "rdma_ports": len(ports),
        "active_roce_ports": active_roce_ports,
        "interfaces": [
            {
                "master": name,
                "prefixes": sorted(prefixes[name]),
                "node_anchor": (net / name / "ifalias")
                .read_text()
                .strip()
                .startswith("foretoken-rdma:"),
            }
            for name in sorted(masters)
        ],
    }


if __name__ == "__main__":
    print(json.dumps(discover_interfaces()))
