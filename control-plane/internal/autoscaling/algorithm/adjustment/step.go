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

// Adjust applies directional stabilization, replica bounds, and a one-replica step per evaluation.
func (step Step) Adjust(input core.AdjustmentInput) (core.ReplicaAdjustment, error) {
	if input.CurrentReplicas < input.Limits.MinReplicas {
		return core.ReplicaAdjustment{Replicas: input.Limits.MinReplicas, Reason: core.AdjustmentReasonScaleUpLimited, Message: "current replicas are below the configured minimum"}, nil
	}
	if input.CurrentReplicas > input.Limits.MaxReplicas {
		return core.ReplicaAdjustment{Replicas: input.Limits.MaxReplicas, Reason: core.AdjustmentReasonScaleDownLimited, Message: "current replicas are above the configured maximum"}, nil
	}

	raw := clip(input.RecommendedReplicas, input.Limits.MinReplicas, input.Limits.MaxReplicas)
	desired := step.config.History.Stabilize(
		input.Target,
		input.EvaluatedAt,
		input.CurrentReplicas,
		raw,
		step.config.ScaleUpStabilizationWindow,
		step.config.ScaleDownStabilizationWindow,
	)
	reason := core.AdjustmentReasonHold
	message := "replica recommendation keeps current replicas"
	if raw > input.CurrentReplicas {
		reason, message = core.AdjustmentReasonStepUp, "replica recommendation increases current replicas"
	} else if raw < input.CurrentReplicas {
		reason, message = core.AdjustmentReasonStepDown, "replica recommendation decreases current replicas"
	}
	if desired != raw {
		if raw > input.CurrentReplicas {
			reason, message = core.AdjustmentReasonScaleUpStabilized, "recent recommendations stabilize scale up"
		} else {
			reason, message = core.AdjustmentReasonScaleDownStabilized, "recent recommendations stabilize scale down"
		}
	}

	current := int64(input.CurrentReplicas)
	if int64(desired) > current+1 {
		desired = int32(current + 1)
		reason, message = core.AdjustmentReasonScaleUpLimited, "replica adjustment is limited to one additional replica"
	} else if int64(desired) < current-1 {
		desired = int32(current - 1)
		reason, message = core.AdjustmentReasonScaleDownLimited, "replica adjustment is limited to one fewer replica"
	}
	return core.ReplicaAdjustment{Replicas: desired, Reason: reason, Message: message}, nil
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
