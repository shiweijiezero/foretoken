<!-- SPDX-License-Identifier: Apache-2.0 -->
<!-- SPDX-FileCopyrightText: Copyright contributors to the Foretoken project -->

# KV locality index

This crate tracks KV prefix locality from normalized lifecycle events emitted by model servers.

Records are isolated by event source, ModelGroup, epoch, DP rank, partition, and storage placement. A route binding resolves an exact event source and rank, then returns only placements that route can read. HostPinned, Disk, and External placements also require the corresponding restore or transfer capability.

## Event handling

- `BlockStored` provides the authoritative parent chain.
- `BlockRemoved` carries hashes only. The index resolves them through retained reverse metadata and fails closed when a hash is unknown.
- `AllBlocksCleared` clears the matching event-source and rank stream.

Device, HostPinned, Disk, and External records remain separate throughout indexing and lookup.

## vLLM normalization

The request-side hash contract is `normalized_keyed_blake3_v1`. The model-server adapter converts vLLM Store hashes and parent chains to this format and retains the reverse mapping needed by Remove events. Foretoken does not treat raw vLLM hashes as normalized hashes.

Storage mappings are:

- `GPU` and `DEVICE` → Device
- `CPU` and `CPU_PINNED` → HostPinned
- `STORAGE`, `DISK`, and `NVME` → Disk
- `REMOTE`, `EXTERNAL`, `NETWORK`, and `SHARED` → External

## Synchronization

The synchronizer validates zero-based sequence continuity before marking a source healthy. Duplicates, gaps, reordered events, and epoch changes are handled without publishing partial state.

Both index implementations share this lifecycle contract. The radix implementation uses `patricia_tree` for compressed-prefix storage; Foretoken adds event, ownership, rank, and placement semantics around it.
