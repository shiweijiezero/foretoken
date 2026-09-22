<!-- SPDX-License-Identifier: Apache-2.0 -->
<!-- SPDX-FileCopyrightText: Copyright contributors to the Foretoken project -->

# Autoscale Model Services

[English](autoscaling.md) | [中文](autoscaling_zh.md)

Configure autoscaling in `ModelService.spec.autoscaling`. The controller uses the selected built-in algorithms; users do not edit CRDs or register algorithms.

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

## Optional parameters

Every stage accepts an `algorithm` and an optional `parameters` object. Omitted parameters use the algorithm defaults.

| Stage | Algorithm | Parameters and defaults |
| --- | --- | --- |
| Decision | `queue` | `targetAverageQueuedRequests: 1` |
| Decision | `queue_threshold` | `scaleUpQueuedRequests: 1`, `scaleDownQueuedRequests: 0` |
| Decision | `aimd` | `additiveIncrease: 1`, `multiplicativeDecreasePercent: 50`, `scaleUpQueuedRequests: 0` |
| Decision | `dynamo_load` | `mode: throughput`, prefill queue thresholds `1/0`, decode KV-cache thresholds `0.8/0.6` |
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

## Dynamo reactive load

Select `dynamo_load` for a Dynamo-compatible reactive policy:

```yaml
decision:
  algorithm: dynamo_load
  parameters:
    mode: throughput
```

Aggregate, encoder, and prefill Pools use queued requests. Decode Pools use
the model-server KV-cache utilization when it is available. `latency` mode
uses lower decode thresholds (`0.4` scale up and `0.1` scale down). The
algorithm fails closed when the required decode metric is unavailable; it does
not interpret a missing metric as zero. The existing trigger, step adjustment,
hard bounds, and lifecycle constraints remain in effect.

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

For the complete maintained workload, see the [multi-model example](../examples/multi-model-quickstart/README.md). The implementation boundary is documented in the [Autoscaling architecture guide](../control-plane/internal/autoscaling/README.md).
