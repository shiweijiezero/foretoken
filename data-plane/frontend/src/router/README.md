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

`kv_least_loaded` prefers longer reusable KV prefixes, then confirmed storage tiers and locality, then current and downstream Decode load. Tiers without identity-complete observations receive no locality preference. `HostPinned` denotes page-locked host memory for offloaded KV. `least_loaded` ranks by load alone; `uniform` assigns equal scores.

`weighted_random` samples using weights derived from the complete score ranking, with equal weights for ties. `max` selects the highest score. `power_of_two_choices` samples two distinct candidates and selects the higher score, breaking ties randomly. With only two candidates it compares both, so it does not prevent traffic concentrating on the higher-scored one.

Set `scorer` to `queue_depth` to prefer fewer requests waiting in the engine scheduler, `running_request` to prefer fewer running requests, or `kv_cache_utilization` to prefer lower measured KV-cache utilization. The Picker applies the selected sampling or maximum-score rule.

Routing distinguishes DP ranks within each model group. Load policies use each rank's current scheduler counts; the utilization policy uses that rank's KV-cache usage. These gauges are available from the first telemetry response, without waiting for a rate window. Missing rank observations are not treated as zero load: measured candidates rank ahead of unknown candidates, while equally unknown candidates remain eligible for the Picker. The three metric-only policies do not add prefix locality, pending dispatches, or downstream-stage load.

A target is eligible only when it is healthy and supports the requested model, input length, and capabilities. For services with separate prefill/decode or encoder/prefill/decode stages, routing keeps the selected stages compatible with one another.

When the KV index reports `Unavailable`, the target remains eligible and receives no KV-prefix preference; routing still considers its load. See the [KV prefix index](../kv-indexer/README.md) for locality and degradation behavior.

For compiled-in routing algorithms and exact Filter, Scorer, and Picker contracts, see [Router maintenance](MAINTAINER.md).
