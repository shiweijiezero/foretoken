// SPDX-License-Identifier: Apache-2.0
// SPDX-FileCopyrightText: Copyright contributors to the Foretoken project
package core

import "fmt"

type Pipeline struct {
	DecisionAlgorithm   DecisionAlgorithm
	TriggerAlgorithm    TriggerAlgorithm
	AdjustmentAlgorithm AdjustmentAlgorithm
	Resolver            Resolver
	Automatic           bool
}

// Plan evaluates each target through trigger, recommendation, adjustment, and resolution stages.
func (pipeline Pipeline) Plan(snapshots []ScalingSnapshot) ([]ScalingDecision, error) {
	if pipeline.DecisionAlgorithm == nil || pipeline.AdjustmentAlgorithm == nil {
		return nil, fmt.Errorf("autoscaler decision and adjustment algorithms are required")
	}
	decisions := make([]ScalingDecision, 0, len(snapshots))
	seen := make(map[TargetID]struct{}, len(snapshots))
	for _, snapshot := range snapshots {
		if _, exists := seen[snapshot.Target]; exists {
			return nil, fmt.Errorf("duplicate autoscaling target %q", snapshot.Target.Name)
		}
		seen[snapshot.Target] = struct{}{}

		var trigger TriggerDecision
		var recommendation ReplicaRecommendation
		if pipeline.Automatic {
			if pipeline.TriggerAlgorithm == nil {
				return nil, fmt.Errorf("automatic autoscaler requires a trigger")
			}
			trigger = pipeline.TriggerAlgorithm.Decide(snapshot)
			switch trigger.Disposition {
			case TriggerFire:
				value, err := pipeline.DecisionAlgorithm.RecommendReplicas(snapshot)
				if err != nil {
					return nil, fmt.Errorf("decision algorithm %q for target %q: %w", pipeline.DecisionAlgorithm.Name(), snapshot.Target.Name, err)
				}
				recommendation = value
			case TriggerInsufficientData:
				recommendation = ReplicaRecommendation{State: RecommendationInsufficientData, Replicas: snapshot.Replicas.RequestedReplicas, Reason: recommendationReason(trigger.Reason), Message: trigger.Message}
			default:
				return nil, fmt.Errorf("autoscaling trigger %q returned invalid disposition %q for target %q", pipeline.TriggerAlgorithm.Name(), trigger.Disposition, snapshot.Target.Name)
			}
		} else {
			value, err := pipeline.DecisionAlgorithm.RecommendReplicas(snapshot)
			if err != nil {
				return nil, fmt.Errorf("decision algorithm %q for target %q: %w", pipeline.DecisionAlgorithm.Name(), snapshot.Target.Name, err)
			}
			recommendation = value
		}

		current := snapshot.Replicas.RequestedReplicas
		outsideBounds := current < snapshot.Limits.MinReplicas || current > snapshot.Limits.MaxReplicas
		adjustment := ReplicaAdjustment{Replicas: current, Reason: AdjustmentReasonHold, Message: "replica adjustment is not required"}
		if recommendation.State == RecommendationAvailable || outsideBounds {
			value, err := pipeline.AdjustmentAlgorithm.Adjust(AdjustmentInput{
				Target:              snapshot.Target,
				EvaluatedAt:         snapshot.EvaluatedAt,
				CurrentReplicas:     current,
				RecommendedReplicas: recommendation.Replicas,
				Limits:              snapshot.Limits,
			})
			if err != nil {
				return nil, fmt.Errorf("adjustment algorithm %q for target %q: %w", pipeline.AdjustmentAlgorithm.Name(), snapshot.Target.Name, err)
			}
			adjustment = value
		}

		result, err := pipeline.Resolver.Resolve(snapshot, pipeline.DecisionAlgorithm.Name(), recommendation, pipeline.AdjustmentAlgorithm.Name(), adjustment)
		if err != nil {
			return nil, err
		}
		if pipeline.Automatic {
			result.Trigger = trigger
		}
		decisions = append(decisions, result)
	}
	return decisions, nil
}

func recommendationReason(reason TriggerReason) RecommendationReason {
	switch reason {
	case TriggerReasonMetricsUnavailable:
		return RecommendationReasonMetricsUnavailable
	case TriggerReasonMetricsStale:
		return RecommendationReasonMetricsStale
	case TriggerReasonMetricsIncomplete:
		return RecommendationReasonMetricsIncomplete
	default:
		return RecommendationReasonStable
	}
}
