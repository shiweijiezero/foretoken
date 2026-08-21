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

func (resolver Resolver) Hold(snapshot ScalingSnapshot, trigger TriggerDecision, decisionName, adjustmentName string, adjustment ScalingAdjustment) (ScalingDecision, error) {
	if err := validateSnapshot(snapshot); err != nil {
		return ScalingDecision{}, err
	}
	if trigger.Disposition != TriggerHold && trigger.Disposition != TriggerInsufficientData {
		return ScalingDecision{}, fmt.Errorf("trigger decision for target %q cannot hold capacity with disposition %q", snapshot.Target.Name, trigger.Disposition)
	}
	current, applied := snapshot.Capacity.RequestedGroups, snapshot.Capacity.RequestedGroups
	constraint, message := DesiredCapacityReason(""), trigger.Message
	if current < snapshot.Limits.MinGroups {
		applied, constraint, message = snapshot.Limits.MinGroups, DesiredCapacityReasonAtMinimum, adjustment.Message
	} else if current > snapshot.Limits.MaxGroups {
		applied, constraint, message = snapshot.Limits.MaxGroups, DesiredCapacityReasonAtMaximum, adjustment.Message
	}
	if applied != current && adjustment.AdjustedGroups != applied {
		return ScalingDecision{}, fmt.Errorf("adjustment algorithm %q returned desired groups %d for target %q instead of hard bound %d", adjustmentName, adjustment.AdjustedGroups, snapshot.Target.Name, applied)
	}
	return ScalingDecision{Target: snapshot.Target, DecisionAlgorithm: decisionName, AdjustmentAlgorithm: adjustmentName, Adjustment: adjustment, AppliedGroups: applied, Direction: direction(current, applied), Constraint: constraint, Message: message, Trigger: trigger}, nil
}
func (resolver Resolver) Resolve(snapshot ScalingSnapshot, decisionName string, desiredCapacity DesiredCapacity, adjustmentName string, adjusted ScalingAdjustment) (ScalingDecision, error) {
	if err := validateSnapshot(snapshot); err != nil {
		return ScalingDecision{}, err
	}
	current, desired := snapshot.Capacity.RequestedGroups, snapshot.Capacity.RequestedGroups
	message := desiredCapacity.Message
	switch desiredCapacity.Disposition {
	case DesiredCapacityApply:
		desired, message = adjusted.AdjustedGroups, adjusted.Message
	case DesiredCapacityHold, DesiredCapacityInsufficientData:
	default:
		return ScalingDecision{}, fmt.Errorf("decision algorithm %q returned invalid disposition %q for target %q", decisionName, desiredCapacity.Disposition, snapshot.Target.Name)
	}
	if desiredCapacity.Disposition == DesiredCapacityApply && (desired < snapshot.Limits.MinGroups || desired > snapshot.Limits.MaxGroups) {
		return ScalingDecision{}, fmt.Errorf("adjustment algorithm %q returned desired groups %d for target %q outside [%d, %d]", adjustmentName, desired, snapshot.Target.Name, snapshot.Limits.MinGroups, snapshot.Limits.MaxGroups)
	}
	constraint := DesiredCapacityReason("")
	// Freeze ordinary changes while replacement capacity is still converging, but allow
	// zero-to-positive bootstrap so a scaled-to-zero target can re-enter service.
	if snapshot.Capacity.Transitioning && !resolver.AllowDuringTransition && !(current == 0 && desired > 0) && desired != current {
		desired, constraint, message = current, DesiredCapacityReasonTransitionInProgress, "capacity transition is in progress; holding current capacity"
	}
	return ScalingDecision{Target: snapshot.Target, DecisionAlgorithm: decisionName, AdjustmentAlgorithm: adjustmentName, DesiredCapacity: desiredCapacity, Adjustment: adjusted, AppliedGroups: desired, Direction: direction(current, desired), Constraint: constraint, Message: message}, nil
}
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
