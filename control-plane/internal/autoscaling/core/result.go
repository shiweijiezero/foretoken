// SPDX-License-Identifier: Apache-2.0
// SPDX-FileCopyrightText: Copyright contributors to the Foretoken project
package core

import "fmt"

type ConstraintReason string

const (
	ConstraintReasonAtMinimum            ConstraintReason = "AtMinimum"
	ConstraintReasonAtMaximum            ConstraintReason = "AtMaximum"
	ConstraintReasonTransitionInProgress ConstraintReason = "TransitionInProgress"
)

type ScalingDecision struct {
	Target              TargetID
	DecisionAlgorithm   string
	AdjustmentAlgorithm string
	Recommendation      ReplicaRecommendation
	Adjustment          ReplicaAdjustment
	AppliedReplicas     int32
	Direction           Direction
	Constraint          ConstraintReason
	Message             string
	Trigger             TriggerDecision
}

type Resolver struct{ AllowDuringTransition bool }

// Resolve applies an adjusted recommendation subject to replica bounds and ModelGroup lifecycle transitions.
func (resolver Resolver) Resolve(snapshot ScalingSnapshot, decisionName string, recommendation ReplicaRecommendation, adjustmentName string, adjustment ReplicaAdjustment) (ScalingDecision, error) {
	if err := validateSnapshot(snapshot); err != nil {
		return ScalingDecision{}, err
	}
	if recommendation.State != RecommendationAvailable && recommendation.State != RecommendationInsufficientData {
		return ScalingDecision{}, fmt.Errorf("decision algorithm %q returned invalid recommendation state %q for target %q", decisionName, recommendation.State, snapshot.Target.Name)
	}

	current := snapshot.Replicas.RequestedReplicas
	applied := current
	message := recommendation.Message
	if recommendation.State == RecommendationAvailable {
		applied, message = adjustment.Replicas, adjustment.Message
	}
	constraint := ConstraintReason("")
	if current < snapshot.Limits.MinReplicas {
		applied, constraint, message = snapshot.Limits.MinReplicas, ConstraintReasonAtMinimum, adjustment.Message
	} else if current > snapshot.Limits.MaxReplicas {
		applied, constraint, message = snapshot.Limits.MaxReplicas, ConstraintReasonAtMaximum, adjustment.Message
	}
	if applied < snapshot.Limits.MinReplicas || applied > snapshot.Limits.MaxReplicas {
		return ScalingDecision{}, fmt.Errorf("adjustment algorithm %q returned replicas %d for target %q outside [%d, %d]", adjustmentName, applied, snapshot.Target.Name, snapshot.Limits.MinReplicas, snapshot.Limits.MaxReplicas)
	}
	if constraint != "" && adjustment.Replicas != applied {
		return ScalingDecision{}, fmt.Errorf("adjustment algorithm %q returned replicas %d for target %q instead of hard bound %d", adjustmentName, adjustment.Replicas, snapshot.Target.Name, applied)
	}
	if constraint == "" && snapshot.Replicas.Transitioning && !resolver.AllowDuringTransition && applied != current {
		applied, constraint, message = current, ConstraintReasonTransitionInProgress, "replica transition is in progress; holding current replicas"
	}
	return ScalingDecision{
		Target:              snapshot.Target,
		DecisionAlgorithm:   decisionName,
		AdjustmentAlgorithm: adjustmentName,
		Recommendation:      recommendation,
		Adjustment:          adjustment,
		AppliedReplicas:     applied,
		Direction:           direction(current, applied),
		Constraint:          constraint,
		Message:             message,
	}, nil
}

func validateSnapshot(snapshot ScalingSnapshot) error {
	if snapshot.Limits.MinReplicas < 0 || snapshot.Limits.MaxReplicas < snapshot.Limits.MinReplicas {
		return fmt.Errorf("autoscaling replica bounds for target %q are invalid", snapshot.Target.Name)
	}
	if snapshot.Replicas.BaselineReplicas < 0 || snapshot.Replicas.RequestedReplicas < 0 {
		return fmt.Errorf("autoscaling replicas for target %q must be non-negative", snapshot.Target.Name)
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
