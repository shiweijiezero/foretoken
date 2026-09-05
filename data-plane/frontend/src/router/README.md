<!-- SPDX-License-Identifier: Apache-2.0 -->
<!-- SPDX-FileCopyrightText: Copyright contributors to the Foretoken project -->

# Router

The Router selects a compatible, healthy model target for each inference request. It does not execute inference, store KV cache, or move cache between instances.

The Foretoken Controller configures the Router through `FrontendService.spec.routerPipeline`:

```yaml
spec:
  routerPipeline:
    filter: allow_all
    scorer: kv_least_loaded
    picker: round_robin
```

| Stage | Current values | Default | Effect |
| --- | --- | --- | --- |
| Filter | `allow_all` | `allow_all` | Retains every compatible, healthy target |
| Scorer | `kv_least_loaded`, `least_loaded`, `uniform` | `kv_least_loaded` | Ranks retained targets |
| Picker | `max`, `round_robin` | `round_robin` | Selects among the highest-scoring targets |

`kv_least_loaded` prefers confirmed local KV-prefix locality, then lower load. `least_loaded` ignores KV locality and ranks by current request load. `uniform` gives every candidate the same score; `round_robin` then rotates deterministically among tied targets, while `max` chooses a deterministic tied target.

A request becomes a candidate only when its model, input limit, requested capabilities, and target health are compatible. The Router evaluates aggregate and disaggregated topologies published by the Controller. In Prefill/Decode and Encoder/Prefill/Decode topologies, it keeps stage selections within their controller-defined pipeline scope.

KV locality is an advisory routing signal. `Unavailable` means the index cannot answer reliably; it is not a cache miss and does not exclude a target. A preferred route is not a guarantee that the inference backend still has the cache when execution begins. See the [KV prefix index](../kv-indexer/README.md) for current KV locality and degradation behavior.

For compiled-in routing algorithms and exact Filter, Scorer, and Picker contracts, see [Router maintenance](MAINTAINER.md).
