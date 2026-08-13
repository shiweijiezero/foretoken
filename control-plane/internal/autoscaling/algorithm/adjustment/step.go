// SPDX-License-Identifier: Apache-2.0
// SPDX-FileCopyrightText: Copyright contributors to the Foretoken project
package adjustment

import (
	"github.com/shiweijiezero/foretoken/control-plane/internal/autoscaling/algorithm"
	"github.com/shiweijiezero/foretoken/control-plane/internal/autoscaling/core"
)

type Step struct{}

func (Step) Name() string { return "step" }
func (Step) Adjust(input core.AdjustmentInput) (core.ScalingAdjustment, error) {
	desired := clip(input.DesiredGroups, input.Bounds.MinGroups, input.Bounds.MaxGroups)
	reason := core.AdjustmentReasonHold
	message := "desired capacity keeps current capacity"
	current := int64(input.CurrentGroups)
	if input.CurrentGroups < input.Bounds.MinGroups {
		return core.ScalingAdjustment{AdjustedGroups: input.Bounds.MinGroups, Reason: core.AdjustmentReasonStepUp, Message: "current capacity is below the configured minimum"}, nil
	}
	if input.CurrentGroups > input.Bounds.MaxGroups {
		return core.ScalingAdjustment{AdjustedGroups: input.Bounds.MaxGroups, Reason: core.AdjustmentReasonStepDown, Message: "current capacity is above the configured maximum"}, nil
	}
	if desired > input.CurrentGroups {
		reason, message = core.AdjustmentReasonStepUp, "desired capacity is limited by the scale-up step"
		if input.Bounds.MaxScaleUpGroups > 0 {
			upper := current + int64(input.Bounds.MaxScaleUpGroups)
			if int64(desired) > upper {
				desired = int32(upper)
			}
		}
	} else if desired < input.CurrentGroups {
		reason, message = core.AdjustmentReasonStepDown, "desired capacity is limited by the scale-down step"
		if input.Bounds.MaxScaleDownGroups > 0 {
			lower := current - int64(input.Bounds.MaxScaleDownGroups)
			if int64(desired) < lower {
				desired = int32(lower)
			}
		}
	}
	return core.ScalingAdjustment{AdjustedGroups: desired, Reason: reason, Message: message}, nil
}
func clip(value, minimum, maximum int32) int32 {
	if value < minimum {
		return minimum
	}
	if value > maximum {
		return maximum
	}
	return value
}
func init() {
	if err := algorithm.RegisterAdjustmentAlgorithm("step", func() (core.AdjustmentAlgorithm, error) { return Step{}, nil }); err != nil {
		panic(err)
	}
}
