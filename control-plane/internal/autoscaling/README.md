# Autoscaling

The public API scales service replicas. The controller materializes each replica as one complete ModelGroup and never scales individual Pods, ranks, or E/P/D members independently.

```text
ScalingSnapshot
→ TriggerDecision
→ ReplicaRecommendation
→ ReplicaAdjustment
→ ScalingDecision
```

- **Trigger** decides whether the current observation can be evaluated. The built-in `periodic` trigger accepts every complete fresh observation supplied by the controller polling loop.
- **Decision** calculates desired capacity. `queue` follows Kubernetes HPA `AverageValue` semantics; `queue_threshold` provides one-replica recommendations at absolute backlog boundaries.
- **Adjustment** applies stabilization, hard bounds, and a fixed one-replica step. A recent higher recommendation delays scale down in the same way as an HPA stabilization window.
- **Resolver** enforces lifecycle ownership. Capacity changes wait for an in-progress ModelGroup transition, while configured min/max bounds remain mandatory.

The public Trigger block may be omitted; it defaults to `periodic` evaluation every five seconds. The controller accepts observations from the latest three polling intervals, so the default freshness limit is 15 seconds. Missing, stale, or incomplete observations hold current capacity and are never interpreted as zero demand. Automatic scaling maintains at least one replica; scale-to-zero is not supported.

## Queue demand

The controller combines two independently owned sources:

```text
runtime preparation queue
+ max(backend dispatch queue, inference scheduler waiting requests)
```

Runtime preparation is independent demand. Backend dispatch and scheduler gauges can briefly observe the same request, so only their larger aggregate is counted. Active model-server requests prevent idle scale down.

## Built-in decisions

### `queue`

```text
desiredReplicas = ceil(queueRequests / targetAverageQueuedRequests)
```

A positive queue can recommend either higher or lower capacity from the average-value formula. With no queued requests, active requests hold current capacity; a fully idle target recommends zero and `minReplicas` supplies the automatic capacity floor.

### `queue_threshold`

```text
queueRequests > scaleUpQueuedRequests
→ recommend currentReplicas + 1

queueRequests <= scaleDownQueuedRequests and activeRequests == 0
→ recommend currentReplicas - 1
```

This mode serves users who reason about absolute service backlog rather than average queue per replica.

## Built-in adjustments

- `direct` applies the Decision recommendation immediately after min/max clipping. It has no stabilization configuration.
- `step` changes at most one replica per trigger and supports independent scale-up and scale-down stabilization windows.

## Example

Top-level `spec.replicas` sets initial capacity. After startup, autoscaling keeps the service within `minReplicas` and `maxReplicas`.

```yaml
autoscaling:
  minReplicas: 1
  maxReplicas: 8
  trigger:
    algorithm: periodic
    interval: 5s
  decision:
    algorithm: queue
    queue:
      targetAverageQueuedRequests: 1
  adjustment:
    algorithm: step
    scaleUp:
      stabilizationWindow: 0s
    scaleDown:
      stabilizationWindow: 300s
```

`desiredReplicas`, `adjustedReplicas`, and `appliedReplicas` in status show the output of Decision, Adjustment, and lifecycle resolution respectively. Trigger, Decision, Adjustment, and Constraint reasons are reported separately.
