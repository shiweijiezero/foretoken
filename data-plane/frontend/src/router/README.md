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
    picker: weighted_random
```

| Stage | Current values | Default | Effect |
| --- | --- | --- | --- |
| Filter | `allow_all` | `allow_all` | Retains every compatible, healthy target |
| Scorer | `kv_least_loaded`, `least_loaded`, `uniform`, `queue_depth`, `running_request`, `kv_cache_utilization` | `kv_least_loaded` | Ranks retained targets using the selected policy |
| Picker | `weighted_random`, `max`, `power_of_two_choices` | `weighted_random` | Samples candidates by descending route-score rank |

Each pipeline stage selects an algorithm by name. Deployments with additional routing implementations can use their names in the same `routerPipeline` fields.

`kv_least_loaded` first prefers longer reusable KV prefixes, then better storage tiers and locality, and finally lower current or downstream Decode load. `HostPinned` means page-locked host memory used for offloaded KV transfer; it is distinct from device memory and disk storage. This preserves local and offloaded KV placement as part of the existing KV-index score. `least_loaded` ignores KV locality and ranks by current request load. `weighted_random` converts the complete `RouteScore` ordering into descending-rank weights and samples one candidate, so a higher-ranked target is more likely without making every request deterministic. `power_of_two_choices` samples two candidates and selects the higher-scored one, reducing full-pool oscillation while retaining load awareness.

Set `scorer` to `queue_depth` to prefer fewer requests waiting in the engine scheduler, `running_request` to prefer fewer running requests, or `kv_cache_utilization` to prefer lower measured KV-cache utilization. Pair these scorers with `weighted_random` for proportional selection or `power_of_two_choices` for lower oscillation.

Routing distinguishes DP ranks within each model group. Load policies use each rank's current scheduler counts; the utilization policy uses that rank's KV-cache usage. These gauges are available from the first telemetry response, without waiting for a rate window. Missing rank observations are not treated as zero load: measured candidates rank ahead of unknown candidates, while equally unknown candidates remain eligible for the Picker. The three metric-only policies do not add prefix locality, pending dispatches, or downstream-stage load.

A target is eligible only when it is healthy and supports the requested model, input length, and capabilities. For services with separate prefill/decode or encoder/prefill/decode stages, routing keeps the selected stages compatible with one another.

When the KV index reports `Unavailable`, the target remains eligible and receives no KV-prefix preference; routing still considers its load. See the [KV prefix index](../kv-indexer/README.md) for locality and degradation behavior.

For compiled-in routing algorithms and exact Filter, Scorer, and Picker contracts, see [Router maintenance](MAINTAINER.md).
