// SPDX-License-Identifier: Apache-2.0
package autoscaling_test

import (
	"context"
	"github.com/shiweijiezero/foretoken/control-plane/internal/autoscaling"
	"github.com/shiweijiezero/foretoken/control-plane/internal/autoscaling/algorithm/adjustment"
	"github.com/shiweijiezero/foretoken/control-plane/internal/autoscaling/core"
	"testing"
)

// TestPipelineCombinesRegisteredStagesAndManualBypassesTelemetry protects automatic stage composition and the telemetry-free manual path.
func TestPipelineCombinesRegisteredStagesAndManualBypassesTelemetry(t *testing.T) {
	snapshot := scalingSnapshot()
	snapshot.Observation.QueueRequests = 5
	automatic, err := autoscaling.New(autoscaling.Configuration{DecisionAlgorithm: autoscaling.DecisionAlgorithmThreshold, TriggerAlgorithm: autoscaling.TriggerAlgorithmPeriodic, AdjustmentAlgorithm: autoscaling.AdjustmentAlgorithmStep, Decision: core.DecisionConfig{ScaleUpQueue: 4}})
	if err != nil {
		t.Fatal(err)
	}
	results, err := automatic.Plan(context.Background(), []core.ScalingSnapshot{snapshot})
	if err != nil {
		t.Fatal(err)
	}
	if len(results) != 1 || results[0].AppliedGroups != 3 || results[0].Trigger.Disposition != core.TriggerFire {
		t.Fatalf("automatic=%#v", results)
	}
	snapshot.Capacity.BaselineGroups = 3
	snapshot.Capacity.RequestedGroups = 1
	snapshot.Observation.State = core.ObservationUnavailable
	manual := autoscaling.Manual()
	results, err = manual.Plan(context.Background(), []core.ScalingSnapshot{snapshot})
	if err != nil || results[0].AppliedGroups != 3 || results[0].Trigger.Disposition != "" {
		t.Fatalf("manual=%#v err=%v", results, err)
	}
}

// TestAutomaticHoldStillEnforcesCapacityBounds protects minimum and maximum capacity while automatic scaling holds.
func TestAutomaticHoldStillEnforcesCapacityBounds(t *testing.T) {
	for _, tc := range []struct {
		name       string
		current    int32
		limits     core.CapacityLimits
		transition bool
		trigger    core.TriggerDecision
		want       int32
		constraint core.DesiredCapacityReason
		direction  core.Direction
	}{
		{name: "hold below minimum", current: 0, limits: core.CapacityLimits{MinGroups: 1, MaxGroups: 8}, trigger: core.TriggerDecision{Disposition: core.TriggerHold, Reason: core.TriggerReasonWithinWatermarkBand}, want: 1, constraint: core.DesiredCapacityReasonAtMinimum, direction: core.DirectionUp},
		{name: "insufficient data above maximum while transitioning", current: 5, limits: core.CapacityLimits{MinGroups: 0, MaxGroups: 3}, transition: true, trigger: core.TriggerDecision{Disposition: core.TriggerInsufficientData, Reason: core.TriggerDesiredCapacityReasonObservationStale}, want: 3, constraint: core.DesiredCapacityReasonAtMaximum, direction: core.DirectionDown},
	} {
		t.Run(tc.name, func(t *testing.T) {
			snapshot := scalingSnapshot()
			snapshot.Capacity.RequestedGroups = tc.current
			snapshot.Capacity.Transitioning = tc.transition
			snapshot.Limits = tc.limits
			pipeline := autoscaling.NewWithAlgorithms(currentCapacityDecision{}, fixedTrigger{tc.trigger}, adjustment.Step{}, true)
			results, err := pipeline.Plan(context.Background(), []core.ScalingSnapshot{snapshot})
			if err != nil {
				t.Fatal(err)
			}
			result := results[0]
			if result.AppliedGroups != tc.want || result.Adjustment.AdjustedGroups != tc.want || result.Constraint != tc.constraint || result.Direction != tc.direction || result.Trigger != tc.trigger {
				t.Fatalf("bounded hold = %#v", result)
			}
		})
	}
}

type fixedTrigger struct{ decision core.TriggerDecision }

func (trigger fixedTrigger) Name() string { return "fixed" }
func (trigger fixedTrigger) Decide(core.ScalingSnapshot) core.TriggerDecision {
	return trigger.decision
}

type currentCapacityDecision struct{}

func (currentCapacityDecision) Name() string { return "hold" }
func (currentCapacityDecision) CalculateDesiredCapacity(_ context.Context, snapshot core.ScalingSnapshot) (core.DesiredCapacity, error) {
	return core.DesiredCapacity{Disposition: core.DesiredCapacityHold, Groups: snapshot.Capacity.RequestedGroups}, nil
}

func scalingSnapshot() core.ScalingSnapshot {
	return core.ScalingSnapshot{ID: "snapshot", Target: core.TargetID{ServiceUID: "service", Name: "default", Kind: core.TargetPool, Role: core.RoleAggregate}, Capacity: core.CapacityState{BaselineGroups: 2, RequestedGroups: 2, RoutableGroups: 1}, Limits: core.CapacityLimits{MinGroups: 0, MaxGroups: 8}, Observation: core.DemandObservation{State: core.ObservationFresh, Window: core.ObservationWindow{Complete: true}}}
}
