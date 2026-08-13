# Autoscaling

Autoscaling operates on complete ModelGroups. It never scales individual Pods, ranks, or E/P/D members independently.

The controller first decides whether the current observations should be evaluated. A decision algorithm then calculates `DesiredCapacity`, the desired number of complete Groups. An adjustment algorithm applies the configured bounds and per-round change limits. Core lifecycle rules produce the final `ScalingDecision`, and the ModelService controller writes the applied capacity to `ModelPool.spec.desiredGroups`.

```text
ScalingSnapshot
→ TriggerDecision
→ DesiredCapacity
→ ScalingAdjustment
→ ScalingDecision
```

A trigger may hold the current capacity before `DesiredCapacity` is calculated. Missing, stale, or incomplete observations also hold current capacity instead of being interpreted as zero demand.

## Output case

For a Pool with two requested Groups and one routable Group, assume the queue observation contains five waiting requests. The built-in queue algorithm requests one additional complete Group, and the step adjustment allows that change in the current round:

```text
ScalingSnapshot:
  target:
    kind: Pool
    name: aggregate
    uid: 8c88ee9a-c10f-41fd-98ef-a09d256b5213
  capacity.requestedGroups: 2
  capacity.routableGroups: 1
  observation.queueRequests: 5
  limits: [1, 8]

TriggerDecision:
  disposition: Fire
  reason: Periodic

DesiredCapacity:
  disposition: Apply
  groups: 3
  reason: QueuePressure

ScalingAdjustment:
  adjustedGroups: 3
  reason: StepUp

ScalingDecision:
  target:
    kind: Pool
    name: aggregate
    uid: 8c88ee9a-c10f-41fd-98ef-a09d256b5213
  appliedGroups: 3
  direction: Up

ModelPool[aggregate].spec.desiredGroups: 2 → 3
```

`DesiredCapacity.groups` is the capacity calculated from demand. `adjustedGroups` and `appliedGroups` show what this reconciliation round applies after adjustment and lifecycle rules.

```text
autoscaling/
├── core/       # fixed inputs, interfaces, pipeline, and result rules
├── algorithm/  # replaceable trigger, decision, and adjustment algorithms
└── tests/      # behavior tests for the public autoscaling contracts
```

Implementations register under stable lower-snake-case names. Empty, duplicate, unknown, or invalid selections return explicit errors.
