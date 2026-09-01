// SPDX-License-Identifier: Apache-2.0
package autoscaling_test

import (
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
		DecisionAlgorithm:   autoscaling.DecisionAlgorithmQueue,
		TriggerAlgorithm:    autoscaling.TriggerAlgorithmPeriodic,
		AdjustmentAlgorithm: autoscaling.AdjustmentAlgorithmStep,
		Decision:            core.DecisionConfig{TargetAverageQueuedRequests: 2},
		Adjustment:          core.AdjustmentConfig{History: core.NewRecommendationHistory()},
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
		DecisionAlgorithm:   autoscaling.DecisionAlgorithmQueue,
		TriggerAlgorithm:    autoscaling.TriggerAlgorithmPeriodic,
		AdjustmentAlgorithm: autoscaling.AdjustmentAlgorithmDirect,
		Decision:            core.DecisionConfig{TargetAverageQueuedRequests: 1},
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
		DecisionAlgorithm:   autoscaling.DecisionAlgorithmQueue,
		TriggerAlgorithm:    autoscaling.TriggerAlgorithmPeriodic,
		AdjustmentAlgorithm: autoscaling.AdjustmentAlgorithmStep,
		Decision:            core.DecisionConfig{TargetAverageQueuedRequests: 1},
		Adjustment:          core.AdjustmentConfig{History: core.NewRecommendationHistory()},
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
		DecisionAlgorithm:   autoscaling.DecisionAlgorithmQueueThreshold,
		TriggerAlgorithm:    autoscaling.TriggerAlgorithmPeriodic,
		AdjustmentAlgorithm: autoscaling.AdjustmentAlgorithmDirect,
		Decision: core.DecisionConfig{
			ScaleUpQueuedRequests:   10,
			ScaleDownQueuedRequests: 0,
		},
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
		DecisionAlgorithm:   autoscaling.DecisionAlgorithmQueue,
		TriggerAlgorithm:    autoscaling.TriggerAlgorithmPeriodic,
		AdjustmentAlgorithm: autoscaling.AdjustmentAlgorithmStep,
		Decision:            core.DecisionConfig{TargetAverageQueuedRequests: 2},
		Adjustment: core.AdjustmentConfig{
			ScaleDownStabilizationWindow: 5 * time.Minute,
			History:                      history,
		},
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

func scalingSnapshot() core.ScalingSnapshot {
	return core.ScalingSnapshot{
		Target:      core.TargetID{ServiceUID: "service", Name: "default", Kind: core.TargetPool, Role: core.RoleAggregate},
		EvaluatedAt: time.Unix(1_000, 0),
		Replicas:    core.ReplicaState{BaselineReplicas: 2, RequestedReplicas: 2, RoutableReplicas: 1},
		Limits:      core.ReplicaLimits{MinReplicas: 1, MaxReplicas: 8},
		Metrics:     core.MetricsSnapshot{State: core.MetricsFresh, Window: core.MetricsWindow{Complete: true}},
	}
}
