// SPDX-License-Identifier: Apache-2.0
package autoscaling_test

import (
	"github.com/shiweijiezero/foretoken/control-plane/internal/autoscaling/algorithm"
	"github.com/shiweijiezero/foretoken/control-plane/internal/autoscaling/core"
	"strings"
	"testing"
)

func TestAdjustmentsAreRegisteredAndResolutionRejectsInvalidOutput(t *testing.T) {
	direct, err := algorithm.BuildAdjustment("direct")
	if err != nil {
		t.Fatal(err)
	}
	got, _ := direct.Adjust(core.AdjustmentInput{CurrentGroups: 2, DesiredGroups: 9, Bounds: core.CapacityLimits{MinGroups: 1, MaxGroups: 4}})
	if got.AdjustedGroups != 4 {
		t.Fatalf("direct=%#v", got)
	}
	step, err := algorithm.BuildAdjustment("step")
	if err != nil {
		t.Fatal(err)
	}
	got, _ = step.Adjust(core.AdjustmentInput{CurrentGroups: 2, DesiredGroups: 8, Bounds: core.CapacityLimits{MinGroups: 0, MaxGroups: 8, MaxScaleUpGroups: 2}})
	if got.AdjustedGroups != 4 {
		t.Fatalf("step=%#v", got)
	}
	got, _ = step.Adjust(core.AdjustmentInput{CurrentGroups: 10, DesiredGroups: 5, Bounds: core.CapacityLimits{MinGroups: 1, MaxGroups: 5, MaxScaleDownGroups: 1}})
	if got.AdjustedGroups != 5 {
		t.Fatalf("lowered maximum=%#v", got)
	}
	got, _ = step.Adjust(core.AdjustmentInput{CurrentGroups: 1, DesiredGroups: 4, Bounds: core.CapacityLimits{MinGroups: 4, MaxGroups: 8, MaxScaleUpGroups: 1}})
	if got.AdjustedGroups != 4 {
		t.Fatalf("raised minimum=%#v", got)
	}
	snapshot := scalingSnapshot()
	snapshot.Capacity.Transitioning = true
	held, err := core.Resolver{}.Resolve(snapshot, "threshold", core.DesiredCapacity{Disposition: core.DesiredCapacityApply, Groups: 3}, "step", core.ScalingAdjustment{AdjustedGroups: 3})
	if err != nil || held.Direction != core.DirectionHold || held.Constraint != core.DesiredCapacityReasonTransitionInProgress {
		t.Fatalf("held=%#v err=%v", held, err)
	}
	_, err = core.Resolver{}.Resolve(snapshot, "threshold", core.DesiredCapacity{Disposition: core.DesiredCapacityApply}, "custom", core.ScalingAdjustment{AdjustedGroups: 9})
	if err == nil || !strings.Contains(err.Error(), "outside [0, 8]") {
		t.Fatalf("invalid adjustment=%v", err)
	}
}
