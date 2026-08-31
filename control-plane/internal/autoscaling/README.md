# Autoscaling

Autoscaling operates on complete ModelGroups. It never scales individual Pods, ranks, or E/P/D members independently.

```text
ScalingSnapshot
→ TriggerDecision
→ DesiredCapacity
→ ScalingAdjustment
→ ScalingDecision
```

- **Trigger** decides whether the current observation can be evaluated. The built-in `periodic` trigger accepts every complete fresh observation supplied by the controller polling loop.
- **Decision** calculates desired capacity. `queue` follows Kubernetes HPA `AverageValue` semantics; `queue_threshold` provides one-Group recommendations at absolute backlog boundaries.
- **Adjustment** applies stabilization, hard bounds, and per-evaluation Group limits. A recent higher recommendation delays scale down in the same way as an HPA stabilization window.
- **Resolver** enforces lifecycle ownership. Capacity changes wait for an in-progress Group transition, while configured min/max bounds remain mandatory.

Missing, stale, or incomplete observations hold current capacity and are never interpreted as zero demand. Automatic scaling maintains at least one Group; scale-to-zero is not supported.

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
desiredGroups = ceil(queueRequests / targetAverageQueuedRequests)
```

A positive queue can recommend either higher or lower capacity from the average-value formula. With no queued requests, active requests hold current capacity; a fully idle target recommends zero and `minGroups` supplies the automatic capacity floor.

### `queue_threshold`

```text
queueRequests > scaleUpQueuedRequests
→ recommend currentGroups + 1

queueRequests <= scaleDownQueuedRequests and activeRequests == 0
→ recommend currentGroups - 1
```

This mode serves users who reason about absolute service backlog rather than average queue per Group.

## Example

```yaml
autoscaling:
  minGroups: 1
  maxGroups: 8
  pollingInterval: 5s
  observationMaxAge: 15s
  decision:
    algorithm: queue
    queue:
      targetAverageQueuedRequests: 1
  adjustment:
    algorithm: step
    scaleUp:
      stabilizationWindow: 0s
      maxGroupsPerEvaluation: 1
    scaleDown:
      stabilizationWindow: 300s
      maxGroupsPerEvaluation: 1
```

`desiredGroups`, `adjustedGroups`, and `appliedGroups` in status show the output of Decision, Adjustment, and lifecycle resolution respectively. Trigger, Decision, Adjustment, and Constraint reasons are reported separately.
