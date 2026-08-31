// SPDX-License-Identifier: Apache-2.0
// SPDX-FileCopyrightText: Copyright contributors to the Foretoken project
package core

import "fmt"

type ScalingDecision struct {
	Target              TargetID
	DecisionAlgorithm   string
	AdjustmentAlgorithm string
	DesiredCapacity     DesiredCapacity
	Adjustment          ScalingAdjustment
	AppliedGroups       int32
	Direction           Direction
	Constraint          DesiredCapacityReason
	Message             string
	Trigger             TriggerDecision
}

type Resolver struct{ AllowDuringTransition bool }

// Resolve applies an adjusted recommendation subject to hard bounds and in-progress lifecycle transitions.
func (resolver Resolver) Resolve(snapshot ScalingSnapshot, decisionName string, desiredCapacity DesiredCapacity, adjustmentName string, adjusted ScalingAdjustment) (ScalingDecision, error) {
	if err := validateSnapshot(snapshot); err != nil {
		return ScalingDecision{}, err
	}
	if desiredCapacity.Disposition != DesiredCapacityApply && desiredCapacity.Disposition != DesiredCapacityInsufficientData {
		return ScalingDecision{}, fmt.Errorf("decision algorithm %q returned invalid disposition %q for target %q", decisionName, desiredCapacity.Disposition, snapshot.Target.Name)
	}

	current := snapshot.Capacity.RequestedGroups
	applied := current
	message := desiredCapacity.Message
	if desiredCapacity.Disposition == DesiredCapacityApply {
		applied, message = adjusted.AdjustedGroups, adjusted.Message
	}
	constraint := DesiredCapacityReason("")
	if current < snapshot.Limits.MinGroups {
		applied, constraint, message = snapshot.Limits.MinGroups, DesiredCapacityReasonAtMinimum, adjusted.Message
	} else if current > snapshot.Limits.MaxGroups {
		applied, constraint, message = snapshot.Limits.MaxGroups, DesiredCapacityReasonAtMaximum, adjusted.Message
	}
	if applied < snapshot.Limits.MinGroups || applied > snapshot.Limits.MaxGroups {
		return ScalingDecision{}, fmt.Errorf("adjustment algorithm %q returned groups %d for target %q outside [%d, %d]", adjustmentName, applied, snapshot.Target.Name, snapshot.Limits.MinGroups, snapshot.Limits.MaxGroups)
	}
	if constraint != "" && adjusted.AdjustedGroups != applied {
		return ScalingDecision{}, fmt.Errorf("adjustment algorithm %q returned groups %d for target %q instead of hard bound %d", adjustmentName, adjusted.AdjustedGroups, snapshot.Target.Name, applied)
	}
	if constraint == "" && snapshot.Capacity.Transitioning && !resolver.AllowDuringTransition && applied != current {
		applied, constraint, message = current, DesiredCapacityReasonTransitionInProgress, "capacity transition is in progress; holding current capacity"
	}
	return ScalingDecision{
		Target:              snapshot.Target,
		DecisionAlgorithm:   decisionName,
		AdjustmentAlgorithm: adjustmentName,
		DesiredCapacity:     desiredCapacity,
		Adjustment:          adjusted,
		AppliedGroups:       applied,
		Direction:           direction(current, applied),
		Constraint:          constraint,
		Message:             message,
	}, nil
}

// validateSnapshot verifies the capacity bounds required by the resolution stage.
func validateSnapshot(snapshot ScalingSnapshot) error {
	if snapshot.Limits.MinGroups < 0 || snapshot.Limits.MaxGroups < snapshot.Limits.MinGroups {
		return fmt.Errorf("autoscaling bounds for target %q are invalid", snapshot.Target.Name)
	}
	if snapshot.Capacity.BaselineGroups < 0 || snapshot.Capacity.RequestedGroups < 0 {
		return fmt.Errorf("autoscaling capacity for target %q must be non-negative", snapshot.Target.Name)
	}
	return nil
}

func direction(current, desired int32) Direction {
	if desired > current {
		return DirectionUp
	}
	if desired < current {
		return DirectionDown
	}
	return DirectionHold
}
