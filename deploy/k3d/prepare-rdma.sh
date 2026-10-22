#!/bin/sh
# SPDX-License-Identifier: Apache-2.0
# SPDX-FileCopyrightText: Copyright contributors to the Foretoken project

# Give nested nodes stable RoCE parents without moving or addressing physical NICs.
set -eu

exec 3</proc/self/ns/net
nsenter --net=/run/foretoken/host-network -- sh -eu -c '
    index=0
    for port in /sys/class/infiniband/*/ports/*; do
        [ -d "$port" ] || continue
        [ "$(cat "$port/link_layer")" = Ethernet ] || continue
        case "$(cat "$port/state")" in "4:"*) ;; *) continue ;; esac
        for entry in "$port"/gid_attrs/ndevs/*; do
            parent=$(cat "$entry" 2>/dev/null) || continue
            if [ -n "$parent" ]; then
                printf "%s\n" "$parent"
            fi
        done
    done | sort -u | while IFS= read -r parent; do
        anchor="ft-rdma$index"
        index=$((index + 1))
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
            ip link set dev "$temporary" netns /proc/self/fd/3
            moved=$temporary
            temporary=
            nsenter --net=/proc/self/fd/3 -- ip link set dev "$moved" name "$anchor"
        fi
        # Only Pod interfaces receive IPAM addresses and advertise routable GIDs.
        nsenter --net=/proc/self/fd/3 -- ip link set dev "$anchor" alias "$identity" addrgenmode none up
    done
'
