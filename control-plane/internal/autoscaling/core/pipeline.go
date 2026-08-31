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

// Plan evaluates each target through trigger, decision, adjustment, and resolution stages.
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
		var desired DesiredCapacity
		if pipeline.Automatic {
			if pipeline.TriggerAlgorithm == nil {
				return nil, fmt.Errorf("automatic autoscaler requires a trigger")
			}
			trigger = pipeline.TriggerAlgorithm.Decide(snapshot)
			switch trigger.Disposition {
			case TriggerFire:
				calculated, err := pipeline.DecisionAlgorithm.CalculateDesiredCapacity(snapshot)
				if err != nil {
					return nil, fmt.Errorf("decision algorithm %q for target %q: %w", pipeline.DecisionAlgorithm.Name(), snapshot.Target.Name, err)
				}
				desired = calculated
			case TriggerInsufficientData:
				desired = DesiredCapacity{Disposition: DesiredCapacityInsufficientData, Groups: snapshot.Capacity.RequestedGroups, Reason: desiredReason(trigger.Reason), Message: trigger.Message}
			default:
				return nil, fmt.Errorf("autoscaling trigger %q returned invalid disposition %q for target %q", pipeline.TriggerAlgorithm.Name(), trigger.Disposition, snapshot.Target.Name)
			}
		} else {
			calculated, err := pipeline.DecisionAlgorithm.CalculateDesiredCapacity(snapshot)
			if err != nil {
				return nil, fmt.Errorf("decision algorithm %q for target %q: %w", pipeline.DecisionAlgorithm.Name(), snapshot.Target.Name, err)
			}
			desired = calculated
		}

		current := snapshot.Capacity.RequestedGroups
		outsideBounds := current < snapshot.Limits.MinGroups || current > snapshot.Limits.MaxGroups
		adjusted := ScalingAdjustment{AdjustedGroups: current, Reason: AdjustmentReasonHold, Message: "capacity adjustment is not required"}
		if desired.Disposition == DesiredCapacityApply || outsideBounds {
			value, err := pipeline.AdjustmentAlgorithm.Adjust(AdjustmentInput{
				Target:        snapshot.Target,
				EvaluatedAt:   snapshot.EvaluatedAt,
				CurrentGroups: current,
				DesiredGroups: desired.Groups,
				Bounds:        snapshot.Limits,
			})
			if err != nil {
				return nil, fmt.Errorf("adjustment algorithm %q for target %q: %w", pipeline.AdjustmentAlgorithm.Name(), snapshot.Target.Name, err)
			}
			adjusted = value
		}

		result, err := pipeline.Resolver.Resolve(snapshot, pipeline.DecisionAlgorithm.Name(), desired, pipeline.AdjustmentAlgorithm.Name(), adjusted)
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

func desiredReason(reason TriggerReason) DesiredCapacityReason {
	switch reason {
	case TriggerReasonObservationUnavailable:
		return DesiredCapacityReasonObservationUnavailable
	case TriggerReasonObservationStale:
		return DesiredCapacityReasonObservationStale
	case TriggerReasonObservationIncomplete:
		return DesiredCapacityReasonObservationIncomplete
	default:
		return DesiredCapacityReasonStable
	}
}
