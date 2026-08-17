<!-- SPDX-License-Identifier: Apache-2.0 -->
<!-- SPDX-FileCopyrightText: Copyright contributors to the Foretoken project -->

# KV prefix index

## What problem it solves

An inference backend creates KV cache while processing a prompt. A later request with the same prompt prefix can reuse that cache instead of computing the prefix again.

The KV prefix index acts as a cache directory: it records which model instance has each prompt prefix. The inference backend still owns the KV cache itself. The index only provides information for Router queries; it does not store or transfer cache or select a route.

## A routing example

Suppose a request contains 1,000 prompt tokens:

- route target A has cached the first 800 tokens;
- route target B has cached the first 200 tokens.

The index reports both matches. The Router may prefer A, or it may select B because of load or another routing condition.

## How it works in Foretoken

```text
Inference backend ──KV block events──> KV prefix index ──prefix matches──> Router
```

- A backend adapter converts backend-specific KV cache events into Foretoken's common events.
- The KV prefix index keeps cache information for each event source, route target, and DP rank separate.
- The Router queries the `KvPrefixIndexer` and combines cache matches with load and other routing signals.

### KV block events

Foretoken maintains the index from three common event types:

- `BlockStored`: a new KV block was stored;
- `BlockRemoved`: a KV block was removed;
- `AllBlocksCleared`: all KV blocks for an event source and DP rank were cleared.

### Cache placements

| Placement | Meaning |
| --- | --- |
| `Device` | Cache on the current compute device, available for direct use |
| `HostPinned` | Cache in host memory that must be restored to the compute device |
| `Disk` | Cache on local storage that must be restored from disk |
| `External` | Cache on remote or shared storage that must be transferred over the network |

The index returns `HostPinned`, `Disk`, or `External` entries only when the route has the corresponding restore or transfer capability.

### Event synchronization

Each event source has its own epoch and continuous sequence. If events are missing or reordered, or the epoch changes, the index temporarily returns `Unavailable` instead of exposing incomplete data. Queries become available again after synchronization recovers.

## Query interface

A `KvPrefixLookup` contains:

- `route_target_id`: the route target to query;
- `data_parallel_rank`: the exact DP rank within that target;
- `prompt_token_ids`: the request's prompt tokens.

A query returns one of the following results:

- `Matches`: the query completed. The result contains reusable prompt-token counts and cache placements; no match is also a valid result.
- `Unavailable`: the index cannot provide a reliable answer, for example because the event source has not synchronized or the request does not support prefix lookup. This is not the same as a cache miss.

## Supporting another inference backend

Backend-specific behavior belongs in an adapter. The adapter converts the backend's events, block identifiers, and storage types into Foretoken's common types; `KvPrefixIndexer` itself does not depend on a particular inference backend.

To support another backend, add or extend its adapter instead of adding backend-specific branches to the index implementations.

## Choosing an index implementation

This crate provides:

- `PositionalHashIndex`, which matches blocks by prompt position;
- `RadixTreeIndex`, which finds matching prefixes with a compressed prefix tree;
- `NoopKvPrefixIndexer`, which returns `Unavailable` when KV prefix lookup is disabled.

All implementations use the `KvPrefixIndexer` interface, so the Router does not need to know which index structure is active.
