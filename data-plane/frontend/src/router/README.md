<!-- SPDX-License-Identifier: Apache-2.0 -->
<!-- SPDX-FileCopyrightText: Copyright contributors to the Foretoken project -->

# Router

The Router selects a compatible, healthy model target for each inference request.

Configure routing in `FrontendService.spec.routerPipeline`:

```yaml
spec:
  routerPipeline:
    filter:
      algorithm: allow_all
    scorer:
      algorithm: kv_least_loaded
    picker:
      algorithm: weighted_random
```

| Stage | Current values | Default | Effect |
| --- | --- | --- | --- |
| Filter | `allow_all` | `allow_all` | Retains every compatible, healthy target |
| Scorer | `kv_least_loaded`, `least_loaded`, `uniform`, `queue_depth`, `running_request`, `kv_cache_utilization`, `active_request`, `token_load`, `prefix` | `kv_least_loaded` | Ranks retained targets |
| Picker | `weighted_random`, `max`, `power_of_two_choices` | `weighted_random` | Samples candidates by descending route-score rank |

`kv_least_loaded` prefers longer reusable KV prefixes, then confirmed storage tiers and locality, then current and downstream Decode load. Tiers without identity-complete observations receive no locality preference. `HostPinned` denotes page-locked host memory for offloaded KV. `least_loaded` ranks by load alone; `uniform` assigns equal scores.

`weighted_random` samples using weights derived from the complete score ranking, with equal weights for ties. `max` selects the highest score. `power_of_two_choices` samples two distinct candidates and selects the higher score, breaking ties randomly. With only two candidates it compares both, so it does not prevent traffic concentrating on the higher-scored one.

Set `scorer.algorithm` to `queue_depth` to prefer fewer requests waiting in the engine scheduler, `running_request` to prefer fewer running requests, or `kv_cache_utilization` to prefer lower measured KV-cache utilization. The Picker applies the selected sampling or maximum-score rule.

Set `scorer.algorithm` to `active_request` to prefer fewer active requests tracked by this frontend. Configure `scorer.parameters.idleThreshold` and `scorer.parameters.maxBusyScore` when the defaults are not suitable.

Set `scorer.algorithm` to `token_load` to prefer candidates with lower frontend-local in-flight token load and lower incoming uncached prompt load. Configure `scorer.parameters.queueThresholdTokens` to change the saturation point.

Set `scorer.algorithm` to `prefix` to prefer candidates with a larger reusable prompt prefix. Configure `scorer.parameters.matchLengthWeight` and `scorer.parameters.matchLengthScaleTokens` to add a normalized match-length preference.

The Router selects only healthy targets that support the requested model, input length, and capabilities. For services with separate prefill/decode or encoder/prefill/decode stages, it keeps the selected stages compatible with one another.

When the KV index is unavailable, targets remain eligible and routing continues without KV-prefix preference. See the [KV prefix index](../kv-indexer/README.md) for cache locality behavior.
