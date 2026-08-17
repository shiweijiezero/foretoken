// SPDX-License-Identifier: Apache-2.0
package autoscaling_test

import (
	"context"
	"errors"
	"github.com/shiweijiezero/foretoken/control-plane/internal/autoscaling"
	"github.com/shiweijiezero/foretoken/control-plane/internal/autoscaling/algorithm"
	"github.com/shiweijiezero/foretoken/control-plane/internal/autoscaling/core"
	"strings"
	"testing"
)

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
	registry := algorithm.NewRegistry()
	if err := registry.RegisterTriggerAlgorithm("test", func(core.TriggerConfig) (core.TriggerAlgorithm, error) { return nil, nil }); err != nil {
		t.Fatal(err)
	}
	if err := registry.RegisterTriggerAlgorithm("test", func(core.TriggerConfig) (core.TriggerAlgorithm, error) { return nil, nil }); err == nil || !strings.Contains(err.Error(), "registered twice") {
		t.Fatalf("duplicate=%v", err)
	}
	if err := registry.RegisterTriggerAlgorithm("", func(core.TriggerConfig) (core.TriggerAlgorithm, error) { return nil, nil }); err == nil || !strings.Contains(err.Error(), "must not be empty") {
		t.Fatalf("empty=%v", err)
	}
	if _, err := registry.BuildTrigger("missing", core.TriggerConfig{}); err == nil || !strings.Contains(err.Error(), "unknown") {
		t.Fatalf("unknown=%v", err)
	}
	if _, err := (*autoscaling.Autoscaler)(nil).Plan(context.Background(), nil); !errors.Is(err, autoscaling.ErrAutoscalerRequired) {
		t.Fatalf("nil autoscaler error=%v", err)
	}

	holdPipeline := autoscaling.NewWithAlgorithms(currentCapacityDecision{}, nil, failAdjustment{}, false)
	results, err = holdPipeline.Plan(context.Background(), []core.ScalingSnapshot{snapshot})
	if err != nil || results[0].Adjustment != (core.ScalingAdjustment{}) {
		t.Fatalf("hold=%#v err=%v", results, err)
	}
}

type currentCapacityDecision struct{}

func (currentCapacityDecision) Name() string { return "hold" }
func (currentCapacityDecision) CalculateDesiredCapacity(_ context.Context, snapshot core.ScalingSnapshot) (core.DesiredCapacity, error) {
	return core.DesiredCapacity{Disposition: core.DesiredCapacityHold, Groups: snapshot.Capacity.RequestedGroups}, nil
}

type failAdjustment struct{}

func (failAdjustment) Name() string { return "fail" }
func (failAdjustment) Adjust(core.AdjustmentInput) (core.ScalingAdjustment, error) {
	return core.ScalingAdjustment{}, errors.New("adjustment must not run")
}

func contains(names []string, wanted string) bool {
	for _, name := range names {
		if name == wanted {
			return true
		}
	}
	return false
}

func scalingSnapshot() core.ScalingSnapshot {
	return core.ScalingSnapshot{ID: "snapshot", Target: core.TargetID{ServiceUID: "service", Name: "default", Kind: core.TargetPool, Role: core.RoleAggregate}, Capacity: core.CapacityState{BaselineGroups: 2, RequestedGroups: 2, RoutableGroups: 1}, Limits: core.CapacityLimits{MinGroups: 0, MaxGroups: 8}, Observation: core.DemandObservation{State: core.ObservationFresh, Window: core.ObservationWindow{Complete: true}}}
}
