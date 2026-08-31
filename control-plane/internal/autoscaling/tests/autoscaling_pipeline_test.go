// SPDX-License-Identifier: Apache-2.0
package autoscaling_test

import (
	"testing"
	"time"

	"github.com/shiweijiezero/foretoken/control-plane/internal/autoscaling"
	"github.com/shiweijiezero/foretoken/control-plane/internal/autoscaling/core"
)

// TestPipelineSeparatesCapacityCalculationFromStepAdjustment protects HPA-style queue recommendations from fixed-step application.
func TestPipelineSeparatesCapacityCalculationFromStepAdjustment(t *testing.T) {
	snapshot := scalingSnapshot()
	snapshot.Capacity.RequestedGroups = 1
	snapshot.Observation.QueueRequests = 5
	planner, err := autoscaling.New(autoscaling.Configuration{
		DecisionAlgorithm:   autoscaling.DecisionAlgorithmQueue,
		TriggerAlgorithm:    autoscaling.TriggerAlgorithmPeriodic,
		AdjustmentAlgorithm: autoscaling.AdjustmentAlgorithmStep,
		Decision:            core.DecisionConfig{TargetAverageQueuedRequests: 2},
		Adjustment: core.AdjustmentConfig{
			MaxScaleUpGroups: 1,
			History:          core.NewRecommendationHistory(),
		},
	})
	if err != nil {
		t.Fatal(err)
	}
	results, err := planner.Plan([]core.ScalingSnapshot{snapshot})
	if err != nil {
		t.Fatal(err)
	}
	result := results[0]
	if result.DesiredCapacity.Groups != 3 || result.Adjustment.AdjustedGroups != 2 || result.AppliedGroups != 2 || result.Direction != core.DirectionUp {
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
	snapshot.Capacity.RequestedGroups = 8
	snapshot.Observation.QueueRequests = 1
	results, err := planner.Plan([]core.ScalingSnapshot{snapshot})
	if err != nil || results[0].DesiredCapacity.Groups != 1 || results[0].AppliedGroups != 1 {
		t.Fatalf("lower queue recommendation = %#v err=%v", results, err)
	}
}

// TestManualCapacityBypassesTelemetry protects fixed replicas from automatic observation requirements.
func TestManualCapacityBypassesTelemetry(t *testing.T) {
	snapshot := scalingSnapshot()
	snapshot.Capacity.BaselineGroups = 3
	snapshot.Capacity.RequestedGroups = 1
	snapshot.Observation.State = core.ObservationUnavailable
	results, err := autoscaling.Manual().Plan([]core.ScalingSnapshot{snapshot})
	if err != nil {
		t.Fatal(err)
	}
	if results[0].AppliedGroups != 3 || results[0].Trigger.Disposition != "" {
		t.Fatalf("manual decision = %#v", results[0])
	}
}

// TestAutomaticInsufficientDataStillEnforcesHardBounds protects configured capacity bounds when telemetry is unavailable.
func TestAutomaticInsufficientDataStillEnforcesHardBounds(t *testing.T) {
	snapshot := scalingSnapshot()
	snapshot.Capacity.RequestedGroups = 0
	snapshot.Limits = core.CapacityLimits{MinGroups: 1, MaxGroups: 8}
	snapshot.Observation.State = core.ObservationUnavailable
	planner, err := autoscaling.New(autoscaling.Configuration{
		DecisionAlgorithm:   autoscaling.DecisionAlgorithmQueue,
		TriggerAlgorithm:    autoscaling.TriggerAlgorithmPeriodic,
		AdjustmentAlgorithm: autoscaling.AdjustmentAlgorithmStep,
		Decision:            core.DecisionConfig{TargetAverageQueuedRequests: 1},
		Adjustment:          core.AdjustmentConfig{MaxScaleUpGroups: 1, MaxScaleDownGroups: 1, History: core.NewRecommendationHistory()},
	})
	if err != nil {
		t.Fatal(err)
	}
	results, err := planner.Plan([]core.ScalingSnapshot{snapshot})
	if err != nil {
		t.Fatal(err)
	}
	result := results[0]
	if result.DesiredCapacity.Disposition != core.DesiredCapacityInsufficientData || result.AppliedGroups != 1 || result.Constraint != core.DesiredCapacityReasonAtMinimum {
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
	snapshot.Observation.QueueRequests = 11
	results, err := planner.Plan([]core.ScalingSnapshot{snapshot})
	if err != nil || results[0].AppliedGroups != 3 {
		t.Fatalf("threshold scale up = %#v err=%v", results, err)
	}

	snapshot.Capacity.RequestedGroups = 3
	snapshot.Observation.QueueRequests = 0
	results, err = planner.Plan([]core.ScalingSnapshot{snapshot})
	if err != nil || results[0].AppliedGroups != 2 {
		t.Fatalf("threshold scale down = %#v err=%v", results, err)
	}
}

// TestScaleDownStabilizationRetainsRecentHigherRecommendation protects burst gaps from immediately removing warm Groups.
func TestScaleDownStabilizationRetainsRecentHigherRecommendation(t *testing.T) {
	history := core.NewRecommendationHistory()
	planner, err := autoscaling.New(autoscaling.Configuration{
		DecisionAlgorithm:   autoscaling.DecisionAlgorithmQueue,
		TriggerAlgorithm:    autoscaling.TriggerAlgorithmPeriodic,
		AdjustmentAlgorithm: autoscaling.AdjustmentAlgorithmStep,
		Decision:            core.DecisionConfig{TargetAverageQueuedRequests: 2},
		Adjustment: core.AdjustmentConfig{
			MaxScaleUpGroups:             1,
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
	loaded.Capacity.RequestedGroups = 1
	loaded.Observation.QueueRequests = 5
	results, err := planner.Plan([]core.ScalingSnapshot{loaded})
	if err != nil || results[0].DesiredCapacity.Groups != 3 || results[0].AppliedGroups != 2 {
		t.Fatalf("loaded decision = %#v err=%v", results, err)
	}

	idle := scalingSnapshot()
	idle.EvaluatedAt = start.Add(time.Second)
	idle.Capacity.RequestedGroups = 2
	results, err = planner.Plan([]core.ScalingSnapshot{idle})
	if err != nil {
		t.Fatal(err)
	}
	if results[0].AppliedGroups != 2 || results[0].Adjustment.Reason != core.AdjustmentReasonScaleDownStabilized {
		t.Fatalf("stabilized decision = %#v", results[0])
	}

	idle.EvaluatedAt = start.Add(5*time.Minute + 2*time.Second)
	results, err = planner.Plan([]core.ScalingSnapshot{idle})
	if err != nil || results[0].AppliedGroups != 1 {
		t.Fatalf("expired stabilization = %#v err=%v", results, err)
	}
}

func scalingSnapshot() core.ScalingSnapshot {
	return core.ScalingSnapshot{
		Target:      core.TargetID{ServiceUID: "service", Name: "default", Kind: core.TargetPool, Role: core.RoleAggregate},
		EvaluatedAt: time.Unix(1_000, 0),
		Capacity:    core.CapacityState{BaselineGroups: 2, RequestedGroups: 2, RoutableGroups: 1},
		Limits:      core.CapacityLimits{MinGroups: 1, MaxGroups: 8},
		Observation: core.DemandObservation{State: core.ObservationFresh, Window: core.ObservationWindow{Complete: true}},
	}
}
