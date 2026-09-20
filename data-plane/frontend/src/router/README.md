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

`kv_least_loaded` prefers longer reusable KV prefixes. For equal lengths it orders confirmed tiers as device, local CPU, local disk, then external Store, before comparing load. Tiers without identity-complete observations receive no locality preference. `least_loaded` ignores KV locality and ranks by current request load. `uniform` gives every candidate the same score; `round_robin` then rotates deterministically among tied targets, while `max` chooses a deterministic tied target.

Set `scorer` to `queue_depth` to prefer fewer requests waiting in the engine scheduler, `running_request` to prefer fewer running requests, or `kv_cache_utilization` to prefer lower measured KV-cache utilization.

Routing distinguishes DP ranks within each model group. Load policies use each rank's current scheduler counts; the utilization policy uses that rank's KV-cache usage. These gauges are available from the first telemetry response, without waiting for a rate window. Missing rank observations are not treated as zero load: measured candidates rank ahead of unknown candidates, while equally unknown candidates remain eligible for the Picker. The three metric-only policies do not add prefix locality, pending dispatches, or downstream-stage load.

The Frontend `/metrics` endpoint reports routing outcomes and latency by `model_name`. Successful
choices also increment `foretoken_router_target_selections_total` with `model_name`, `model_role`,
`route_target_id`, and `data_parallel_rank`. This counter records the routing decision when a target
is selected; later backend admission, streaming, cancellation, or generation failure does not rewrite
that decision. Model and target labels come only from the active serving configuration.

A target is eligible only when it is healthy and supports the requested model, input length, and capabilities. For services with separate prefill/decode or encoder/prefill/decode stages, routing keeps the selected stages compatible with one another.

When the KV index reports `Unavailable`, the target remains eligible and receives no KV-prefix preference; routing still considers its load. See the [KV prefix index](../kv-indexer/README.md) for locality and degradation behavior.

For compiled-in routing algorithms and exact Filter, Scorer, and Picker contracts, see [Router maintenance](MAINTAINER.md).
