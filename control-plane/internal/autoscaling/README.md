# Autoscaling Architecture

[English](README.md) | [中文](README_zh.md)

This package turns controller-owned observations into `ModelPool` capacity. Users configure autoscaling through `ModelService.spec.autoscaling`; configuration and status usage are documented in the [autoscaling guide](../../../../docs/autoscaling.md).

## Ownership

The `ModelService` controller owns scheduling, observation collection, target discovery, status publication, and writing capacity to `ModelPool`. Algorithms are side-effect-free: they only evaluate one complete observation and return a recommendation.

An aggregate target scales one Pool. An E/P/D target scales one `EPDPipelineScope`, applying the same capacity to its encoder, prefill, and decode Pools.

## Evaluation pipeline

```text
controller polling loop
→ ScalingSnapshot
→ TriggerDecision
→ ReplicaRecommendation
→ ReplicaAdjustment
→ ScalingDecision
→ ModelPool capacity and ModelService status
```

The controller supplies complete, fresh observations to the pipeline. `periodic` accepts those observations; it does not own an interval or requeue loop. The resolver applies hard min/max bounds even when observations are missing, and holds capacity while a target is transitioning.

`step` stabilization uses recent recommendations retained by the current controller process. The history is intentionally runtime-local, so a restart or leader change does not restore a pending scale-down delay.

## Extension boundary

Built-in algorithms live under `algorithm/`. Trigger, decision, and adjustment implementations return domain results and do not read Kubernetes resources, mutate capacity, or schedule work. Add a new implementation only when it represents a current, independently owned recommendation policy; controller lifecycle behavior remains in `core` and the ModelService reconciler.

Each trigger, decision, and adjustment stage owns a compiled descriptor list. Add an implementation and one descriptor in its stage; the top-level registry constructs the selected factory and does not accept runtime registrations. Algorithms own their parameter defaults and validation, while `core.DecodeParameters` provides shared field and type decoding without exposing implementation structs as configuration.

All stages receive an optional JSON parameters object. Adjustment factories also receive the controller-owned recommendation history. Trigger implementations expose their polling interval; the controller owns scheduling and derives observation freshness from that interval. Capacity bounds and lifecycle constraints remain platform responsibilities.

The controller constructs the pipeline once per reconciliation. Built-in defaults select periodic triggering and step adjustment when those stages are omitted; individual parameter defaults remain in the selected implementation. A new implementation requires rebuilding and deploying the controller, not runtime plugin loading.

## Validation

Use the control-plane verification target after changing this package:

```bash
make -C control-plane verify
```
