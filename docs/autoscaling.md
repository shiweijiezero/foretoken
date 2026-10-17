<!-- SPDX-License-Identifier: Apache-2.0 -->
<!-- SPDX-FileCopyrightText: Copyright contributors to the Foretoken project -->

# Autoscale Model Services

[English](autoscaling.md) | [中文](autoscaling_zh.md)

Configure autoscaling in `ModelService.spec.autoscaling`.

## Minimal configuration

Add this block to a `ModelService`:

```yaml
spec:
  replicas: 1
  autoscaling:
    minReplicas: 1
    maxReplicas: 8
    decision:
      algorithm: queue
```

This evaluates queue demand every five seconds and changes at most one replica per evaluation. `periodic` triggering and `step` adjustment are the defaults, so they can be omitted.

To pause the service without changing its scaling settings, use the [stop and resume commands](../cli/README.md#stop-and-resume).

## Optional parameters

Every stage accepts an `algorithm` and an optional `parameters` object. Omitted parameters use the algorithm defaults.

| Stage | Algorithm | Parameters and defaults |
| --- | --- | --- |
| Decision | `queue` | `targetAverageQueuedRequests: 1` |
| Decision | `queue_threshold` | `scaleUpQueuedRequests: 1`, `scaleDownQueuedRequests: 0` |
| Decision | `aimd` | `additiveIncrease: 1`, `multiplicativeDecreasePercent: 50`, `scaleUpQueuedRequests: 0` |
| Decision | `token_load` | `mode: throughput`; choose `latency` for lower thresholds or `load` for custom thresholds |
| Trigger | `periodic` | `interval: 5s` |
| Adjustment | `step` | `scaleUpStabilizationWindow: 0s`, `scaleDownStabilizationWindow: 300s` |
| Adjustment | `direct` | No parameters |

For example, change the polling interval and scale-down window:

```yaml
trigger:
  algorithm: periodic
  parameters:
    interval: 10s
adjustment:
  algorithm: step
  parameters:
    scaleDownStabilizationWindow: 60s
```

Unknown algorithms and invalid parameters produce a `ScalingFailed` condition on the `ModelService`.

## Token-load thresholds

Use `token_load` to scale from token demand rather than request counts:

```yaml
decision:
  algorithm: token_load
```

The default `throughput` mode allows more queued work; `latency` uses lower thresholds to expand sooner. Set `decision.parameters.mode` to choose a mode.

| Mode | Prefill up / down | Decode and aggregate up / down |
| --- | --- | --- |
| `throughput` | `>= 1` / `< 0.1` | `> 1` / `< 0.6` |
| `latency` | `>= 0.1` / `<= 0` | `> 0.4` / `< 0.1` |

Prefill compares queued input tokens divided by the model's context length. Decode compares scheduled and queued context tokens divided by KV token capacity; aggregate serving also includes queued input tokens. Demand can exceed capacity, giving a ratio above 1.

Each data-parallel engine is evaluated separately. One engine meeting the scale-up condition requests one more replica. Removing a replica requires every engine to meet the down threshold and the estimated redistributed load to stay below the up threshold.

For custom thresholds, choose `load` and supply all four parameters:

```yaml
decision:
  algorithm: token_load
  parameters:
    mode: load
    prefillScaleUpQueueTokens: 8192
    prefillScaleDownQueueTokens: 0
    decodeScaleUpTokenRatio: 0.8
    decodeScaleDownTokenRatio: 0.6
```

In this mode, prefill thresholds are token counts and decode/aggregate thresholds are ratios in `[0, 1]`. Each up threshold must exceed its down threshold; expansion uses `>=` and contraction uses `<=`. Custom thresholds require `load` mode.

Token-load observations support a single full-attention KV cache group with context parallelism disabled (PCP/DCP=1). Encoder pools and targets with unavailable, incomplete, or stale observations retain their current capacity within the configured replica bounds.

## AIMD

Select AIMD with:

```yaml
decision:
  algorithm: aimd
```

AIMD adds `additiveIncrease` replicas when the queue exceeds `scaleUpQueuedRequests`. When there are no waiting or active requests, it keeps `multiplicativeDecreasePercent` of the current capacity. The selected adjustment algorithm and service min/max limits still apply.

## Observe autoscaling

Autoscaling results are published in `.status.autoscaling[]`:

```bash
kubectl get modelservice <name> -o json \
  | jq '.status.autoscaling[] | {
      id,
      direction,
      desiredReplicas: .decision.desiredReplicas,
      adjustedReplicas: .adjustment.adjustedReplicas,
      appliedReplicas,
      constraint: .constraint.reason
    }'
```

`desiredReplicas` is the algorithm recommendation. `adjustedReplicas` includes adjustment rules, and `appliedReplicas` is the capacity written after service constraints.

For the complete maintained workload, see the [multi-model example](../examples/quickstart/multi-model/README.md). The implementation boundary is documented in the [Autoscaling architecture guide](../control-plane/internal/autoscaling/README.md).
