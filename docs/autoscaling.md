<!-- SPDX-License-Identifier: Apache-2.0 -->
<!-- SPDX-FileCopyrightText: Copyright contributors to the Foretoken project -->

# Autoscale Model Services

[English](autoscaling.md) | [中文](autoscaling_zh.md)

Autoscaling changes the capacity of a `ModelService` from request demand. Configure it in the service manifest, then inspect the service status while a workload is running.

## Capacity units

For an aggregate model service, each replica runs the complete model with its configured resources and parallelism. For a service with separate encoder, prefill, and decode stages (E/P/D), one replica includes all three stages, which scale together. Its resource requirements are the sum of the resources configured for those stages.

`spec.replicas` provides the baseline capacity. When `autoscaling` is present, `minReplicas` and `maxReplicas` constrain the capacity created from the first reconciliation onward.

## Configure queue autoscaling

Add this `spec` fragment to an existing `ModelService`. It starts at one replica, maintains one to eight replicas, evaluates recent queue demand every five seconds, and changes at most one replica per evaluation:

```yaml
spec:
  replicas: 1
  autoscaling:
    minReplicas: 1
    maxReplicas: 8
    decision:
      algorithm: queue
```

`periodic` evaluates queue demand at the configured interval. Missing, stale, or incomplete observations keep the current capacity. Automatic scaling maintains at least one replica.

`queue` calculates capacity from the average queued requests per replica. `queue_threshold` instead changes capacity by one replica at configured total-backlog boundaries. `direct` applies a recommendation after min/max bounds; `step` applies at most one replica per evaluation and supports independent stabilization windows.

The scale-down window uses recent recommendations held by the current controller process. A controller restart or leadership change does not preserve that history, so it can shorten a pending scale-down delay.

## Algorithm parameters

All three stages use `algorithm` and optional `parameters`. Omit parameters to use the selected algorithm's defaults. Omit the trigger or adjustment stage entirely to use `periodic` or `step`. Algorithm constructors reject unknown fields, wrong types, and invalid values before capacity is written. New algorithms are compiled into the controller; users select them in the service manifest without editing CRDs.

| Algorithm | Parameter | Default | Constraint |
| --- | --- | --- | --- |
| `queue` | `targetAverageQueuedRequests` | `1` | Positive integer |
| `queue_threshold` | `scaleUpQueuedRequests` | `1` | Non-negative integer |
| `queue_threshold` | `scaleDownQueuedRequests` | `0` | Non-negative integer, no greater than `scaleUpQueuedRequests` |
| `aimd` | `additiveIncrease` | `1` | Integer from 1 to 2147483647 |
| `aimd` | `multiplicativeDecreasePercent` | `50` | Integer from 1 to 99; percentage retained when idle |
| `aimd` | `scaleUpQueuedRequests` | `0` | Non-negative integer |
| `periodic` (trigger) | `interval` | `5s` | Positive duration |
| `step` (adjustment) | `scaleUpStabilizationWindow` | `0s` | Non-negative duration |
| `step` (adjustment) | `scaleDownStabilizationWindow` | `300s` | Non-negative duration |
| `direct` (adjustment) | None | — | No parameters accepted |

The controller must contain the named algorithm. Unknown algorithm names or invalid parameters produce a `ScalingFailed` condition on the ModelService; Kubernetes validates the parameters object shape, while the selected algorithm validates its contents.

For example, override the polling interval and scale-down window in the existing `autoscaling` block:

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

## Use additive growth and multiplicative idle reduction

To select AIMD, replace the existing autoscaling decision block with:

```yaml
decision:
  algorithm: aimd
```

AIMD adds `additiveIncrease` to the requested capacity when total waiting requests exceed `scaleUpQueuedRequests`. When both waiting and active requests are zero, it retains `multiplicativeDecreasePercent` of the current capacity, rounding down. Otherwise it holds capacity. For example, 5 idle replicas with the default 50 percent retention produce a recommendation of 2 replicas.

Recommendations still pass through the selected adjustment and lifecycle limits. The default `step` adjustment limits each ordinary evaluation to one replica and retains its scale-down stabilization window; use `direct` when the full AIMD recommendation should apply immediately within min/max and transition constraints.

## Observe a decision

Autoscaling results are published in `.status.autoscaling[]`, one entry for each scaling target. Query the maintained multi-model example with:

```bash
kubectl get modelservice multi-model-qwen3-0.6b \
  --namespace foretoken-multi-model-demo \
  -o json | jq '.status.autoscaling[] | {
    id,
    kind,
    role,
    observationState,
    direction,
    desiredReplicas: .decision.desiredReplicas,
    adjustedReplicas: .adjustment.adjustedReplicas,
    appliedReplicas,
    constraint: .constraint.reason
  }'
```

`desiredReplicas` is the algorithm recommendation. `adjustedReplicas` is the result after stabilization and rate limiting. `appliedReplicas` is the capacity written to the target after lifecycle and min/max constraints. `observationState`, the stage reasons, and `constraint` explain why capacity was held or changed.

For aggregate services, `kind` is `Pool`. For E/P/D services, `kind` is `EPDPipelineScope` and `role` is `EPD`.

## Try the maintained example

The [multi-model example](../examples/multi-model-quickstart/README.md) deploys one queue-autoscaled Qwen service and one fixed-capacity Llama service. It includes a bounded concurrent workload and status commands for observing capacity changes.

## Migrate the previous configuration

Before upgrading, save the existing service manifests and move algorithm-specific values into their stage's `parameters` object:

| Previous field | New field |
| --- | --- |
| `decision.queue.*` / `decision.queueThreshold.*` | `decision.parameters.*` |
| `trigger.interval` | `trigger.parameters.interval` |
| `adjustment.scaleUp.stabilizationWindow` | `adjustment.parameters.scaleUpStabilizationWindow` |
| `adjustment.scaleDown.stabilizationWindow` | `adjustment.parameters.scaleDownStabilizationWindow` |

Remove the old fields. Unchanged default-valued parameters can be omitted. Upgrade the controller and CRDs together, and apply the migrated manifests before allowing the new controller to reconcile existing services. The new schema does not retain the old fields, so unmigrated settings can be lost and replaced by algorithm defaults. Rollback requires the previous controller, CRDs, and saved service manifests together.

## Maintainer architecture

The controller stages, observation aggregation, algorithm extension boundary, and lifecycle resolver are documented in [the autoscaling maintainer README](../control-plane/internal/autoscaling/README.md).
