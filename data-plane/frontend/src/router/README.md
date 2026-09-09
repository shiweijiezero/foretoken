<!-- SPDX-License-Identifier: Apache-2.0 -->
<!-- SPDX-FileCopyrightText: Copyright contributors to the Foretoken project -->

# Router

The Router selects a compatible, healthy model target for each inference request.

Configure routing in `FrontendService.spec.routerPipeline`:

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

Each pipeline stage selects an algorithm by name. Deployments with additional routing implementations can use their names in the same `routerPipeline` fields.

`kv_least_loaded` prefers confirmed local KV-prefix locality, then lower load. `least_loaded` ignores KV locality and ranks by current request load. `uniform` gives every candidate the same score; `round_robin` then rotates deterministically among tied targets, while `max` chooses a deterministic tied target.

A request becomes a candidate only when its model, input limit, requested capabilities, and target health are compatible. The Router evaluates aggregate and disaggregated topologies published by the Controller. In Prefill/Decode and Encoder/Prefill/Decode topologies, it keeps stage selections within a controller-defined connector compatibility scope. A scope can contain multiple ModelGroups for each stage; it does not pair groups by ordinal.

When the KV index reports `Unavailable`, the target remains eligible and receives no KV-prefix preference; routing still considers its load. See the [KV prefix index](../kv-indexer/README.md) for locality and degradation behavior.

For compiled-in routing algorithms and exact Filter, Scorer, and Picker contracts, see [Router maintenance](MAINTAINER.md).
