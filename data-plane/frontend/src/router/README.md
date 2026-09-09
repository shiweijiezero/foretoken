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
| Scorer | `kv_least_loaded`, `least_loaded`, `uniform`, `queue_depth`, `running_request`, `kv_cache_utilization` | `kv_least_loaded` | Ranks retained targets |
| Picker | `max`, `round_robin` | `round_robin` | Selects among the highest-scoring targets |

Each pipeline stage selects an algorithm by name. Deployments with additional routing implementations can use their names in the same `routerPipeline` fields.

`kv_least_loaded` prefers confirmed local KV-prefix locality, then lower load. `least_loaded` ignores KV locality and ranks by current request load. `uniform` gives every candidate the same score; `round_robin` then rotates deterministically among tied targets, while `max` chooses a deterministic tied target.

Set `scorer` to `queue_depth` to prefer fewer requests waiting in the engine scheduler, `running_request` to prefer fewer running requests, or `kv_cache_utilization` to prefer lower measured KV-cache utilization.

These policies use current Model Server endpoint gauges. They do not add prefix locality,
pending dispatches, or downstream-stage load. All DP ranks of one Model Server share its
endpoint score; these policies do not distinguish load between ranks. Gauges are usable after
the first telemetry response, without waiting for the rate observation window. An unobserved gauge is treated
as zero. Later omissions preserve the previous value while its measured snapshot remains in retained
history. After a timestamp or counter reset, omitted gauges remain unobserved until reported again.

A target is eligible only when it is healthy and supports the requested model, input length, and capabilities. For services with separate prefill/decode or encoder/prefill/decode stages, routing keeps the selected stages compatible with one another.

When the KV index reports `Unavailable`, the target remains eligible and receives no KV-prefix preference; routing still considers its load. See the [KV prefix index](../kv-indexer/README.md) for locality and degradation behavior.

For compiled-in routing algorithms and exact Filter, Scorer, and Picker contracts, see [Router maintenance](MAINTAINER.md).
