<!-- SPDX-License-Identifier: Apache-2.0 -->
<!-- SPDX-FileCopyrightText: Copyright contributors to the Foretoken project -->

# KV Prefix Index

The KV prefix index gives the Router cache-locality observations for prompt prefixes. It is a directory, not a cache: the inference backend owns KV blocks, and the index neither stores, restores, nor transfers them.

## Current behavior

The current Controller projection publishes only local `Device` KV locality. Foretoken does not currently use CPU-memory or disk offload, remote cache sharing, or peer-to-peer cache transfer as a routing capability.

For an eligible request, the index can report a confirmed prefix match, a confirmed miss, or `Unavailable`. `Unavailable` means the index cannot answer reliably; it is not a miss. The Router treats it as no KV-locality preference and continues ordinary routing.

A locality match is advisory. It represents complete token-block prefixes within the exact model revision, KV scope, and runtime partition. It is not a guarantee that the backend still has the cache when inference begins. Requests using cache salts, LoRA, unsupported multimodal features, or explicit prefix-cache opt-out do not use KV-prefix lookup.

## Operations

The Frontend refreshes KV locality from model-server events. If an event source is unavailable, lacks its key, or reports an inconsistent cursor or epoch, serving continues but KV-aware routing becomes unavailable or degraded for that source.

Platform operators can inspect the Frontend's cluster-local `/statusz` endpoint for KV-index state and source health, and `/metrics` for Prometheus scraping. Application clients do not need to repair KV synchronization; investigate the model-server event source and serving configuration with the platform operator.

The Router uses this signal with load and request compatibility. See the [Router guide](../router/README.md) for routing behavior.

Generic placement vocabulary, event sequencing, index implementations, and backend adapter requirements are documented for maintainers in [KV index maintenance](MAINTAINER.md).
