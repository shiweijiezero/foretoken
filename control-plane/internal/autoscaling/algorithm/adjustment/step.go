// SPDX-License-Identifier: Apache-2.0
// SPDX-FileCopyrightText: Copyright contributors to the Foretoken project
package adjustment

import (
	"github.com/shiweijiezero/foretoken/control-plane/internal/autoscaling/algorithm"
	"github.com/shiweijiezero/foretoken/control-plane/internal/autoscaling/core"
)

type Step struct{ config core.AdjustmentConfig }

// Name identifies the stabilized fixed-step adjustment algorithm for registry consumers.
func (Step) Name() string { return "step" }

// Adjust applies directional stabilization, hard bounds, and per-evaluation Group limits.
func (step Step) Adjust(input core.AdjustmentInput) (core.ScalingAdjustment, error) {
	if input.CurrentGroups < input.Bounds.MinGroups {
		return core.ScalingAdjustment{AdjustedGroups: input.Bounds.MinGroups, Reason: core.AdjustmentReasonScaleUpLimited, Message: "current capacity is below the configured minimum"}, nil
	}
	if input.CurrentGroups > input.Bounds.MaxGroups {
		return core.ScalingAdjustment{AdjustedGroups: input.Bounds.MaxGroups, Reason: core.AdjustmentReasonScaleDownLimited, Message: "current capacity is above the configured maximum"}, nil
	}

	raw := clip(input.DesiredGroups, input.Bounds.MinGroups, input.Bounds.MaxGroups)
	desired := step.config.History.Stabilize(
		input.Target,
		input.EvaluatedAt,
		input.CurrentGroups,
		raw,
		step.config.ScaleUpStabilizationWindow,
		step.config.ScaleDownStabilizationWindow,
	)
	reason := core.AdjustmentReasonHold
	message := "desired capacity keeps current capacity"
	if raw > input.CurrentGroups {
		reason, message = core.AdjustmentReasonStepUp, "desired capacity increases current capacity"
	} else if raw < input.CurrentGroups {
		reason, message = core.AdjustmentReasonStepDown, "desired capacity decreases current capacity"
	}
	if desired != raw {
		if raw > input.CurrentGroups {
			reason, message = core.AdjustmentReasonScaleUpStabilized, "recent recommendations stabilize scale up"
		} else {
			reason, message = core.AdjustmentReasonScaleDownStabilized, "recent recommendations stabilize scale down"
		}
	}

	current := int64(input.CurrentGroups)
	if desired > input.CurrentGroups {
		if step.config.MaxScaleUpGroups > 0 {
			upper := current + int64(step.config.MaxScaleUpGroups)
			if int64(desired) > upper {
				desired = int32(upper)
				reason, message = core.AdjustmentReasonScaleUpLimited, "desired capacity is limited by the scale-up step"
			}
		}
	} else if desired < input.CurrentGroups {
		if step.config.MaxScaleDownGroups > 0 {
			lower := current - int64(step.config.MaxScaleDownGroups)
			if int64(desired) < lower {
				desired = int32(lower)
				reason, message = core.AdjustmentReasonScaleDownLimited, "desired capacity is limited by the scale-down step"
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
	if err := algorithm.RegisterAdjustmentAlgorithm("step", func(config core.AdjustmentConfig) (core.AdjustmentAlgorithm, error) {
		return Step{config: config}, nil
	}); err != nil {
		panic(err)
	}
}
