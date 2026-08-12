// SPDX-License-Identifier: Apache-2.0
// SPDX-FileCopyrightText: Copyright contributors to the Foretoken project

// Applies non-pluggable autoscaling safety rules to algorithm recommendations.

package core

import (
	"context"
	"fmt"

	"github.com/shiweijiezero/foretoken/control-plane/internal/autoscaling/algorithm"
)

const maxReasonLength = 256

// Decision preserves the algorithm proposal and the core-constrained target.
type Decision struct {
	Target         algorithm.TargetID
	Algorithm      string
	Recommendation algorithm.Recommendation
	AppliedGroups  int32
	Direction      algorithm.Direction
	Constraint     algorithm.RecommendationReason
	Message        string
}

// Evaluator owns identity fencing, bounds, transition gating, and rate limiting.
type Evaluator struct {
	Algorithm             algorithm.Algorithm
	AllowDuringTransition bool
}

func (evaluator Evaluator) Evaluate(ctx context.Context, snapshot algorithm.Snapshot) (Decision, error) {
	if evaluator.Algorithm == nil {
		return Decision{}, fmt.Errorf("autoscaling algorithm is required")
	}
	if snapshot.Target.ServiceUID == "" || snapshot.Target.Name == "" || snapshot.Target.Kind == "" || snapshot.Target.Role == "" {
		return Decision{}, fmt.Errorf("autoscaling target identity is incomplete")
	}
	if snapshot.Ref.ID == "" {
		return Decision{}, fmt.Errorf("autoscaling snapshot ID for target %q is required", snapshot.Target.Name)
	}
	if snapshot.Limits.MinGroups < 0 || snapshot.Limits.MaxGroups < snapshot.Limits.MinGroups {
		return Decision{}, fmt.Errorf("autoscaling bounds for target %q are invalid", snapshot.Target.Name)
	}
	if snapshot.Capacity.BaselineGroups < 0 || snapshot.Capacity.RequestedGroups < 0 {
		return Decision{}, fmt.Errorf("autoscaling capacity for target %q must be non-negative", snapshot.Target.Name)
	}

	recommendation, err := evaluator.Algorithm.Recommend(ctx, snapshot)
	if err != nil {
		return Decision{}, fmt.Errorf("autoscaling algorithm %q for target %q: %w", evaluator.Algorithm.Name(), snapshot.Target.Name, err)
	}
	if recommendation.Target != snapshot.Target {
		return Decision{}, fmt.Errorf("autoscaling algorithm %q returned a mismatched target for %q", evaluator.Algorithm.Name(), snapshot.Target.Name)
	}
	if recommendation.SnapshotID != snapshot.Ref.ID {
		return Decision{}, fmt.Errorf("autoscaling algorithm %q returned a stale snapshot fence for target %q", evaluator.Algorithm.Name(), snapshot.Target.Name)
	}

	current := snapshot.Capacity.RequestedGroups
	desired := current
	constraint := algorithm.RecommendationReason("")
	message := boundedReason(recommendation.Message)
	switch recommendation.Disposition {
	case algorithm.RecommendationApply:
		desired = recommendation.DesiredGroups
	case algorithm.RecommendationHold:
	case algorithm.RecommendationInsufficientData:
	default:
		return Decision{}, fmt.Errorf("autoscaling algorithm %q returned invalid disposition %q for target %q", evaluator.Algorithm.Name(), recommendation.Disposition, snapshot.Target.Name)
	}

	scaleFromZero := current == 0 && desired > 0
	if snapshot.Capacity.Transitioning && !evaluator.AllowDuringTransition && !scaleFromZero && desired != current {
		desired = current
		constraint = algorithm.ReasonTransitionInProgress
		message = "capacity is still converging"
	}

	desired64 := int64(clamp(desired, snapshot.Limits.MinGroups, snapshot.Limits.MaxGroups))
	if snapshot.Limits.MaxScaleUpStep > 0 {
		upper := int64(current) + int64(snapshot.Limits.MaxScaleUpStep)
		if desired64 > upper {
			desired64 = upper
		}
	}
	if snapshot.Limits.MaxScaleDownStep > 0 {
		lower := int64(current) - int64(snapshot.Limits.MaxScaleDownStep)
		if desired64 < lower {
			desired64 = lower
		}
	}
	desired64 = clamp64(desired64, int64(snapshot.Limits.MinGroups), int64(snapshot.Limits.MaxGroups))
	applied := int32(desired64)
	return Decision{
		Target:         snapshot.Target,
		Algorithm:      evaluator.Algorithm.Name(),
		Recommendation: recommendation,
		AppliedGroups:  applied,
		Direction:      direction(current, applied),
		Constraint:     constraint,
		Message:        boundedReason(message),
	}, nil
}

func clamp(value, minimum, maximum int32) int32 {
	if value < minimum {
		return minimum
	}
	if value > maximum {
		return maximum
	}
	return value
}

func clamp64(value, minimum, maximum int64) int64 {
	if value < minimum {
		return minimum
	}
	if value > maximum {
		return maximum
	}
	return value
}

func direction(current, desired int32) algorithm.Direction {
	if desired > current {
		return algorithm.DirectionUp
	}
	if desired < current {
		return algorithm.DirectionDown
	}
	return algorithm.DirectionHold
}

func boundedReason(reason string) string {
	if len(reason) <= maxReasonLength {
		return reason
	}
	return reason[:maxReasonLength]
}
