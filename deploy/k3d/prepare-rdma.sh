#!/bin/sh
# SPDX-License-Identifier: Apache-2.0
# SPDX-FileCopyrightText: Copyright contributors to the Foretoken project

# Give nested nodes stable RoCE parents without moving or addressing physical NICs.
set -eu

state=/var/lib/foretoken/rdma-interfaces
mkdir -p "$state"
# Preserve anchors already present when adopting an existing node namespace.
for interface in /sys/class/net/ft-rdma*; do
    [ -f "$interface/ifalias" ] || continue
    identity=$(cat "$interface/ifalias")
    case "$identity" in foretoken-rdma:*) ;; *) continue ;; esac
    anchor=${interface##*/}
    if [ ! -f "$state/$anchor" ]; then
        printf "%s\n" "${identity#foretoken-rdma:}" > "$state/.$anchor.tmp"
        mv "$state/.$anchor.tmp" "$state/$anchor"
    fi
done
exec 3</proc/self/ns/net
nsenter --net=/run/foretoken/host-network -- sh -eu -c '
    state=$1
    # Netlink follows the entered netns; the existing sysfs mount may not.
    {
        # Node-local names outlive link state and are removed with the k3d node.
        for record in "$state"/ft-rdma*; do
            [ -f "$record" ] || continue
            parent=$(cat "$record")
            if ip link show dev "$parent" >/dev/null 2>&1; then
                printf "%s\n" "$parent"
            fi
        done
        for port in /sys/class/infiniband/*/ports/*; do
            [ -d "$port" ] || continue
            # Require an IB_CA Ethernet port with GID capacity, not an RNIC or raw-only port.
            case "$(cat "${port%/ports/*}/node_type")" in "1:"*) ;; *) continue ;; esac
            [ "$(cat "$port/link_layer")" = Ethernet ] || continue
            [ -f "$port/gids/0" ] || continue
            for entry in "$port"/gid_attrs/ndevs/*; do
                parent=$(cat "$entry" 2>/dev/null) || continue
                if [ -n "$parent" ] && ip link show dev "$parent" >/dev/null 2>&1; then
                    printf "%s\n" "$parent"
                fi
            done
        done
    } | sort -u | while IFS= read -r parent; do
        anchor=
        for record in "$state"/ft-rdma*; do
            if [ -f "$record" ] && [ "$(cat "$record")" = "$parent" ]; then
                anchor=${record##*/}
                break
            fi
        done
        if [ -z "$anchor" ]; then
            index=0
            while [ -f "$state/ft-rdma$index" ]; do
                index=$((index + 1))
            done
            anchor="ft-rdma$index"
            printf "%s\n" "$parent" > "$state/.$anchor.tmp"
            mv "$state/.$anchor.tmp" "$state/$anchor"
        fi
        identity="foretoken-rdma:$parent"
        if nsenter --net=/proc/self/fd/3 -- ip link show dev "$anchor" >/dev/null 2>&1; then
            current=$(nsenter --net=/proc/self/fd/3 -- cat "/sys/class/net/$anchor/ifalias")
            if [ "$current" != "$identity" ]; then
                printf "RoCE interface name is already in use: %s\n" "$anchor" >&2
                exit 1
            fi
        else
            temporary="ft$(cut -c1-12 /proc/sys/kernel/random/uuid | tr -d -)"
            ip link add link "$parent" name "$temporary" type macvlan mode bridge
            trap '\''[ -z "$temporary" ] || ip link delete dev "$temporary"'\'' EXIT
            ip link set dev "$temporary" alias "$identity" addrgenmode none
            ip link set dev "$temporary" netns /proc/self/fd/3 name "$anchor"
            temporary=
        fi
        # Only Pod interfaces receive IPAM addresses and advertise routable GIDs.
        nsenter --net=/proc/self/fd/3 -- ip link set dev "$anchor" alias "$identity" addrgenmode none up
    done
' sh "$state"
