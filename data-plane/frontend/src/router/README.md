<!-- SPDX-License-Identifier: Apache-2.0 -->
<!-- SPDX-FileCopyrightText: Copyright contributors to the Foretoken project -->

# Request Routing

English | [简体中文](README_zh.md)

Route requests to healthy replicas that support the model, input length, and required capabilities. Routing also keeps the stages of prefill/decode and encoder/prefill/decode services compatible.

## Select a routing strategy

To prefer replicas with fewer waiting requests, set the scorer in your `FrontendService` YAML. For the Quick Start, edit `examples/quickstart/frontend.yaml`:

```yaml
spec:
  routerPipeline:
    scorer:
      algorithm: queue_depth
```

Apply the edited configuration:

```bash
foretoken deploy examples/quickstart --timeout 20m
```

The default strategy combines cache reuse and load balancing with `kv_least_loaded` and selects a target with `gamble_sampling`.

Choose a scorer for the workload:

| Goal | Scorer |
| --- | --- |
| Combine prompt-cache reuse with load balancing | `kv_least_loaded` (default) |
| Prefer lower load | `least_loaded` |
| Prefer fewer waiting or running requests | `queue_depth` or `running_request`, respectively |
| Prefer lower measured KV-cache occupancy | `kv_cache_utilization` |
| Balance requests active in this frontend replica | `active_request` |
| Prefer shorter model-server waiting queues | `load_aware` |
| Balance uncached prompt-token load in this frontend replica | `token_load` |
| Prefer reusable prompt prefixes | `prefix` |
| Spread cold requests toward less recently selected targets | `no_hit_lru` |
| Trade off load balance and cache reuse | `two_tier`; requires `picker.algorithm: max` |
| Prefer the same model replica for requests with the same `session_id` in the request body | `session_affinity` |
| Give every target an equal score | `uniform` |

Scorer-specific options go under `scorer.parameters`. For example, give matched prefix length more weight:

```yaml
spec:
  routerPipeline:
    scorer:
      algorithm: prefix
      parameters:
        matchLengthWeight: 0.5
```

When the KV index is unavailable, targets remain eligible without a cache preference. See the [KV prefix index](../kv-indexer/README.md) for supported caches and status access.

## Choose from the scores

Set `routerPipeline.picker.algorithm` to control selection:

| Picker | Selection |
| --- | --- |
| `gamble_sampling` (default) | Higher ranks are more likely; ties have equal probability and lower-ranked targets remain eligible. |
| `max` | Select the highest score. |
| `power_of_two_choices` | Sample two distinct targets and select the higher score; choose randomly on ties. |

## Draft/Target routing

For DT services, Router first selects a Target with healthy compatible Draft pools
from `speculation.draftPools`, matching candidate format, context and support for
the Target's default candidate count, then selects a Draft through the same configured
Filter → Scorer → Picker pipeline. Both routing-load reservations last through
the request. Native workload snapshots inform existing load scorers; they do not
reserve token work or dictate physical batching. The binding is fixed for the
request, and the selected Target advances its rounds. See the
[DT frontend workflow](../../../dt-plugin/docs/frontend-workflow.md).

## Inspect routing behavior

Use the Routing decisions panels in [Grafana](../../../../observability/README.md) to compare selection shares, outcomes, eligible targets, and selection latency while sending traffic.

Request limits and queueing are configured separately through the frontend's [admission rules](../../README.md#configure-admission-rules).
