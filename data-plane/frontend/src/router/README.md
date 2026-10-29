<!-- SPDX-License-Identifier: Apache-2.0 -->
<!-- SPDX-FileCopyrightText: Copyright contributors to the Foretoken project -->

# Request Routing

English | [简体中文](README_zh.md)

Route requests to healthy replicas that support the model, input length, and required capabilities. Routing also keeps the stages of prefill/decode and encoder/prefill/decode services compatible.

## Select a routing strategy

To prefer replicas with fewer waiting requests, set the scorer in your `FrontendService` YAML. For the Quick Start, edit `examples/quickstart/qwen3-0.6b/frontend.yaml`:

```yaml
spec:
  routerPipeline:
    scorer:
      algorithm: queue_depth
```

Apply the edited configuration:

```bash
foretoken deploy examples/quickstart/qwen3-0.6b --timeout 20m
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
| Prefer lower request-length-normalized prefill and decode latency | `least_latency`; requires `picker.algorithm: max` |
| Prefer lower weighted request lengths | `throughput` |
| Balance prefill work and active prompt blocks after cache credits | `kv_cost`; requires `picker.algorithm: max` |
| Prefer a configured endpoint observation | `endpoint_attribute` |
| Give every target an equal score | `uniform` |

`least_latency` sends exploratory requests to idle ranks without latency observations, including newly added replicas.

Scorer-specific options go under `scorer.parameters`. For example, give cache affinity more weight in the default strategy:

```yaml
spec:
  routerPipeline:
    scorer:
      algorithm: kv_least_loaded
      parameters:
        cacheWeight: 0.7
```

`kv_least_loaded` combines cache credit with per-rank request pressure. By default, GPU-local reuse receives full credit and the additional prefix restored from CPU, filesystem, a shared store or another instance's device cache receives half credit. Equal shared-cache hits therefore retain each target's local-cache advantage, while load can change the ranking.

Both weights accept values from 0 to 1:

| Parameter | Default | Effect |
| --- | --- | --- |
| `cacheWeight` | `0.5` | Balances cache affinity against load; `0` selects by load alone and skips cache queries for this scorer. |
| `restoreWeight` | `0.5` | Credit for the restored extension relative to GPU-local reuse; `0` credits only GPU-local reuse, and `1` gives both equal credit. |

For Encoder and Decode, `kv_least_loaded` uses load alone. These weights control routing preference. [Decode-to-Prefill reuse](../../../../docs/features/inference-parameters.md#prefilldecode-separation-and-kv-reuse) remains available with load-only scorers or `cacheWeight: 0`, for both Mooncake and NIXL.

A target with unavailable prefix observations remains eligible without a cache preference. See the [KV prefix index](../kv-indexer/README.md) for supported caches and status access.

## Balance prefill and KV load

Select `kv_cost` with the `max` picker to balance pending prefill work and active KV allocations in this frontend replica:

```yaml
spec:
  routerPipeline:
    scorer:
      algorithm: kv_cost
    picker:
      algorithm: max
```

Costs use token units across engine block sizes and discount reusable prefixes. Set options under `scorer.parameters`; numeric values must be finite and non-negative.

| Parameter | Default | Effect |
| --- | --- | --- |
| `overlap_score_credit` | `1.0` | Multiplier for device-local prefix credit. Setting `0` selects load-only scoring and skips cache queries for this scorer. |
| `overlap_score_credit_decay` | `0.0` | Reduces device credit as a target's pending prefill load exceeds the least-loaded eligible target, relative to the incoming prompt length; `0` disables decay. |
| `prefill_load_scale` | `1.0` | Weight of pending and incoming prefill tokens after cache credits. |
| `decode_active_request_weight` | `0.0` | Additional token-equivalent cost per active request. |
| `host_cache_hit_weight` | `0.75` | Credit for the host-cached prefix extension beyond device hits. |
| `disk_cache_hit_weight` | `0.25` | Credit for the disk-cached extension beyond faster local tiers. |
| `shared_cache_multiplier` | `0.0` | Credit for a shared-cache prefix extension beyond device hits. |
| `router_track_prefill_tokens` | `true` | Include pending prompt work on Aggregate and Prefill targets. |
| `router_track_output_blocks` | `false` | Extend each active sequence's allocation estimate with streamed output tokens. |

## Choose from the scores

Set `routerPipeline.picker.algorithm` to control selection:

| Picker | Selection |
| --- | --- |
| `gamble_sampling` (default) | Higher ranks are more likely; ties have equal probability and lower-ranked targets remain eligible. |
| `max` | Select the highest score, then the smallest target ID. Within that target, prefer the least loaded rank and choose randomly on equal load. |
| `power_of_two_choices` | Sample two distinct targets and select the higher score; choose randomly on ties. |

## Override a model or execution stage

Set `spec.routerPipeline` in the model's `ModelService` YAML to override the frontend defaults. For example, use load-based routing for the model and cache-aware routing for its Prefill stage:

```yaml
spec:
  routerPipeline:
    scorer:
      algorithm: least_loaded
    prefill:
      scorer:
        algorithm: kv_least_loaded
        parameters:
          cacheWeight: 0.7
    decode:
      scorer:
        algorithm: queue_depth
      picker:
        algorithm: max
```

Each of `filter`, `scorer`, and `picker` is resolved independently: an `encoder`, `prefill`, or `decode` selection takes precedence over the model selection, which takes precedence over the frontend default. Aggregate serving uses the model selection. Remove a selection to resume inheritance.

A selection must name its `algorithm` and replaces that algorithm's entire `parameters` object. Omitting parameters uses the selected algorithm's defaults, not the inherited parameters.

Apply changes with the deployment's usual `foretoken deploy` command. Routing updates online without replacing model instances; invalid settings keep the active configuration. ModelServices exposing the same public model name through one frontend must resolve to the same routing configuration.

## Inspect routing behavior

Use the Routing decisions panels in [Grafana](../../../../observability/README.md) to compare selection shares, outcomes, eligible targets, and selection latency while sending traffic.

Request limits and queueing are configured separately through the frontend's [admission rules](../../README.md#configure-admission-rules).
