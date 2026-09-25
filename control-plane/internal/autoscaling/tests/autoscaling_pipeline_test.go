// SPDX-License-Identifier: Apache-2.0
package autoscaling_test

import (
	"encoding/json"
	"testing"
	"time"

	"github.com/shiweijiezero/foretoken/control-plane/internal/autoscaling"
	"github.com/shiweijiezero/foretoken/control-plane/internal/autoscaling/core"
)

// TestPipelineSeparatesReplicaRecommendationFromAdjustment protects HPA-style queue recommendations from fixed-step application.
func TestPipelineSeparatesReplicaRecommendationFromAdjustment(t *testing.T) {
	snapshot := scalingSnapshot()
	snapshot.Replicas.RequestedReplicas = 1
	snapshot.Metrics.WaitingRequests = 5
	planner, err := autoscaling.New(autoscaling.Configuration{
		Decision:   autoscaling.AlgorithmConfiguration{Algorithm: "queue", Parameters: json.RawMessage(`{"targetAverageQueuedRequests":2}`)},
		Adjustment: autoscaling.AlgorithmConfiguration{Algorithm: "step"},
		History:    core.NewRecommendationHistory(),
	})
	if err != nil {
		t.Fatal(err)
	}
	results, err := planner.Plan([]core.ScalingSnapshot{snapshot})
	if err != nil {
		t.Fatal(err)
	}
	result := results[0]
	if result.Recommendation.Replicas != 3 || result.Adjustment.Replicas != 2 || result.AppliedReplicas != 2 || result.Direction != core.DirectionUp {
		t.Fatalf("queue recommendation = %#v", result)
	}
}

// TestQueueAverageValueCanRecommendLowerCapacity protects the full HPA AverageValue formula under sustained low queue.
func TestQueueAverageValueCanRecommendLowerCapacity(t *testing.T) {
	planner, err := autoscaling.New(autoscaling.Configuration{
		Decision:   autoscaling.AlgorithmConfiguration{Algorithm: "queue", Parameters: json.RawMessage(`{"targetAverageQueuedRequests":1}`)},
		Adjustment: autoscaling.AlgorithmConfiguration{Algorithm: "direct"},
	})
	if err != nil {
		t.Fatal(err)
	}
	snapshot := scalingSnapshot()
	snapshot.Replicas.RequestedReplicas = 8
	snapshot.Metrics.WaitingRequests = 1
	results, err := planner.Plan([]core.ScalingSnapshot{snapshot})
	if err != nil || results[0].Recommendation.Replicas != 1 || results[0].AppliedReplicas != 1 {
		t.Fatalf("lower queue recommendation = %#v err=%v", results, err)
	}
}

// TestManualCapacityBypassesTelemetry protects fixed replicas from automatic metrics requirements.
func TestManualCapacityBypassesTelemetry(t *testing.T) {
	snapshot := scalingSnapshot()
	snapshot.Replicas.BaselineReplicas = 3
	snapshot.Replicas.RequestedReplicas = 1
	snapshot.Metrics.State = core.MetricsUnavailable
	results, err := autoscaling.Manual().Plan([]core.ScalingSnapshot{snapshot})
	if err != nil {
		t.Fatal(err)
	}
	if results[0].AppliedReplicas != 3 || results[0].Trigger.Disposition != "" {
		t.Fatalf("manual decision = %#v", results[0])
	}
}

// TestAutomaticInsufficientDataStillEnforcesHardBounds protects configured capacity bounds when telemetry is unavailable.
func TestAutomaticInsufficientDataStillEnforcesHardBounds(t *testing.T) {
	snapshot := scalingSnapshot()
	snapshot.Replicas.RequestedReplicas = 0
	snapshot.Limits = core.ReplicaLimits{MinReplicas: 1, MaxReplicas: 8}
	snapshot.Metrics.State = core.MetricsUnavailable
	planner, err := autoscaling.New(autoscaling.Configuration{
		Decision:   autoscaling.AlgorithmConfiguration{Algorithm: "queue", Parameters: json.RawMessage(`{"targetAverageQueuedRequests":1}`)},
		Adjustment: autoscaling.AlgorithmConfiguration{Algorithm: "step"},
		History:    core.NewRecommendationHistory(),
	})
	if err != nil {
		t.Fatal(err)
	}
	results, err := planner.Plan([]core.ScalingSnapshot{snapshot})
	if err != nil {
		t.Fatal(err)
	}
	result := results[0]
	if result.Recommendation.State != core.RecommendationInsufficientData || result.AppliedReplicas != 1 || result.Constraint != core.ConstraintReasonAtMinimum {
		t.Fatalf("bounded insufficient data = %#v", result)
	}
}

// TestQueueThresholdUsesAbsoluteBacklogBoundaries protects the independent fixed-backlog user policy.
func TestQueueThresholdUsesAbsoluteBacklogBoundaries(t *testing.T) {
	planner, err := autoscaling.New(autoscaling.Configuration{
		Decision:   autoscaling.AlgorithmConfiguration{Algorithm: "queue_threshold", Parameters: json.RawMessage(`{"scaleUpQueuedRequests":10,"scaleDownQueuedRequests":0}`)},
		Adjustment: autoscaling.AlgorithmConfiguration{Algorithm: "direct"},
	})
	if err != nil {
		t.Fatal(err)
	}
	snapshot := scalingSnapshot()
	snapshot.Metrics.WaitingRequests = 11
	results, err := planner.Plan([]core.ScalingSnapshot{snapshot})
	if err != nil || results[0].AppliedReplicas != 3 {
		t.Fatalf("threshold scale up = %#v err=%v", results, err)
	}

	snapshot.Replicas.RequestedReplicas = 3
	snapshot.Metrics.WaitingRequests = 0
	results, err = planner.Plan([]core.ScalingSnapshot{snapshot})
	if err != nil || results[0].AppliedReplicas != 2 {
		t.Fatalf("threshold scale down = %#v err=%v", results, err)
	}
}

// TestScaleDownStabilizationRetainsRecentHigherRecommendation protects burst gaps from immediately removing warm replicas.
func TestScaleDownStabilizationRetainsRecentHigherRecommendation(t *testing.T) {
	history := core.NewRecommendationHistory()
	planner, err := autoscaling.New(autoscaling.Configuration{
		Decision:   autoscaling.AlgorithmConfiguration{Algorithm: "queue", Parameters: json.RawMessage(`{"targetAverageQueuedRequests":2}`)},
		Adjustment: autoscaling.AlgorithmConfiguration{Algorithm: "step"},
		History:    history,
	})
	if err != nil {
		t.Fatal(err)
	}
	start := time.Unix(1_000, 0)
	loaded := scalingSnapshot()
	loaded.EvaluatedAt = start
	loaded.Replicas.RequestedReplicas = 1
	loaded.Metrics.WaitingRequests = 5
	results, err := planner.Plan([]core.ScalingSnapshot{loaded})
	if err != nil || results[0].Recommendation.Replicas != 3 || results[0].AppliedReplicas != 2 {
		t.Fatalf("loaded decision = %#v err=%v", results, err)
	}

	idle := scalingSnapshot()
	idle.EvaluatedAt = start.Add(time.Second)
	idle.Replicas.RequestedReplicas = 2
	results, err = planner.Plan([]core.ScalingSnapshot{idle})
	if err != nil {
		t.Fatal(err)
	}
	if results[0].AppliedReplicas != 2 || results[0].Adjustment.Reason != core.AdjustmentReasonScaleDownStabilized {
		t.Fatalf("stabilized decision = %#v", results[0])
	}

	idle.EvaluatedAt = start.Add(5*time.Minute + 2*time.Second)
	results, err = planner.Plan([]core.ScalingSnapshot{idle})
	if err != nil || results[0].AppliedReplicas != 1 {
		t.Fatalf("expired stabilization = %#v err=%v", results, err)
	}
}

// TestDynamoLoadUsesRoleSpecificTelemetry protects the reactive path used by
// prefill queues and decode KV-cache pressure without adding CRD-specific fields.
func TestDynamoLoadUsesRoleSpecificTelemetry(t *testing.T) {
	planner, err := autoscaling.New(autoscaling.Configuration{
		Decision:   autoscaling.AlgorithmConfiguration{Algorithm: "dynamo_load"},
		Adjustment: autoscaling.AlgorithmConfiguration{Algorithm: "direct"},
	})
	if err != nil {
		t.Fatal(err)
	}

	prefill := scalingSnapshot()
	prefill.Target.Role = core.RolePrefill
	prefill.Replicas.RequestedReplicas = 1
	prefill.Metrics.WaitingRequests = 2
	results, err := planner.Plan([]core.ScalingSnapshot{prefill})
	if err != nil || results[0].Recommendation.Replicas != 2 || results[0].AppliedReplicas != 2 {
		t.Fatalf("Dynamo prefill decision = %#v err=%v", results, err)
	}

	decode := scalingSnapshot()
	decode.Target.Role = core.RoleDecode
	decode.Replicas.RequestedReplicas = 1
	usage := 0.9
	decode.Metrics.KVCacheUsage = &usage
	results, err = planner.Plan([]core.ScalingSnapshot{decode})
	if err != nil || results[0].Recommendation.Replicas != 2 || results[0].AppliedReplicas != 2 {
		t.Fatalf("Dynamo decode decision = %#v err=%v", results, err)
	}
}

// TestDynamoLoadDoesNotTreatMissingKVAsIdle protects fail-closed decode scaling
// when a model server cannot provide the metric required by the policy.
func TestDynamoLoadDoesNotTreatMissingKVAsIdle(t *testing.T) {
	planner, err := autoscaling.New(autoscaling.Configuration{
		Decision:   autoscaling.AlgorithmConfiguration{Algorithm: "dynamo_load"},
		Adjustment: autoscaling.AlgorithmConfiguration{Algorithm: "direct"},
	})
	if err != nil {
		t.Fatal(err)
	}
	snapshot := scalingSnapshot()
	snapshot.Target.Role = core.RoleDecode
	snapshot.Replicas.RequestedReplicas = 2
	results, err := planner.Plan([]core.ScalingSnapshot{snapshot})
	if err != nil {
		t.Fatal(err)
	}
	if results[0].Recommendation.State != core.RecommendationInsufficientData || results[0].AppliedReplicas != 2 {
		t.Fatalf("missing decode telemetry = %#v", results[0])
	}
}

// TestDynamoLoadLatencyDefaultsUseLowerDecodeThreshold protects the mode-owned
// default so users can select latency behavior without copying internal values.
func TestDynamoLoadLatencyDefaultsUseLowerDecodeThreshold(t *testing.T) {
	planner, err := autoscaling.New(autoscaling.Configuration{
		Decision: autoscaling.AlgorithmConfiguration{
			Algorithm:  "dynamo_load",
			Parameters: json.RawMessage(`{"mode":"latency"}`),
		},
		Adjustment: autoscaling.AlgorithmConfiguration{Algorithm: "direct"},
	})
	if err != nil {
		t.Fatal(err)
	}
	snapshot := scalingSnapshot()
	snapshot.Target.Role = core.RoleDecode
	usage := 0.5
	snapshot.Metrics.KVCacheUsage = &usage
	results, err := planner.Plan([]core.ScalingSnapshot{snapshot})
	if err != nil {
		t.Fatal(err)
	}
	if results[0].Recommendation.Replicas != 3 {
		t.Fatalf("latency decode threshold = %#v", results[0])
	}
}

func scalingSnapshot() core.ScalingSnapshot {
	return core.ScalingSnapshot{
		Target:      core.TargetID{ServiceUID: "service", Name: "default", Kind: core.TargetPool, Role: core.RoleAggregate},
		EvaluatedAt: time.Unix(1_000, 0),
		Replicas:    core.ReplicaState{BaselineReplicas: 2, RequestedReplicas: 2, RoutableReplicas: 1},
		Limits:      core.ReplicaLimits{MinReplicas: 1, MaxReplicas: 8},
		Metrics:     core.MetricsSnapshot{State: core.MetricsFresh, Window: core.MetricsWindow{Complete: true}},
	}
}
