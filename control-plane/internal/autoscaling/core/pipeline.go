// SPDX-License-Identifier: Apache-2.0
// SPDX-FileCopyrightText: Copyright contributors to the Foretoken project
package core

import (
	"context"
	"fmt"
)

type Pipeline struct {
	DecisionAlgorithm   DecisionAlgorithm
	TriggerAlgorithm    TriggerAlgorithm
	AdjustmentAlgorithm AdjustmentAlgorithm
	Resolver            Resolver
	Automatic           bool
}

func (pipeline Pipeline) Plan(ctx context.Context, snapshots []ScalingSnapshot) ([]ScalingDecision, error) {
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
		if pipeline.Automatic {
			if pipeline.TriggerAlgorithm == nil {
				return nil, fmt.Errorf("automatic autoscaler requires a trigger")
			}
			trigger = pipeline.TriggerAlgorithm.Decide(snapshot)
			switch trigger.Disposition {
			case TriggerFire:
			case TriggerHold, TriggerInsufficientData:
				// Holding algorithmic demand still passes through hard-bound adjustment and
				// resolution; absence of a demand signal does not suspend capacity safety.
				var adjustment ScalingAdjustment
				current := snapshot.Capacity.RequestedGroups
				if current < snapshot.Limits.MinGroups || current > snapshot.Limits.MaxGroups {
					adjusted, err := pipeline.AdjustmentAlgorithm.Adjust(AdjustmentInput{CurrentGroups: current, DesiredGroups: current, Bounds: snapshot.Limits})
					if err != nil {
						return nil, fmt.Errorf("adjustment algorithm %q for target %q: %w", pipeline.AdjustmentAlgorithm.Name(), snapshot.Target.Name, err)
					}
					adjustment = adjusted
				}
				result, err := pipeline.Resolver.Hold(snapshot, trigger, pipeline.DecisionAlgorithm.Name(), pipeline.AdjustmentAlgorithm.Name(), adjustment)
				if err != nil {
					return nil, err
				}
				decisions = append(decisions, result)
				continue
			default:
				return nil, fmt.Errorf("autoscaling trigger %q returned invalid disposition %q for target %q", pipeline.TriggerAlgorithm.Name(), trigger.Disposition, snapshot.Target.Name)
			}
		}
		desiredCapacity, err := pipeline.DecisionAlgorithm.CalculateDesiredCapacity(ctx, snapshot)
		if err != nil {
			return nil, fmt.Errorf("decision algorithm %q for target %q: %w", pipeline.DecisionAlgorithm.Name(), snapshot.Target.Name, err)
		}
		var adjusted ScalingAdjustment
		if desiredCapacity.Disposition == DesiredCapacityApply {
			adjusted, err = pipeline.AdjustmentAlgorithm.Adjust(AdjustmentInput{CurrentGroups: snapshot.Capacity.RequestedGroups, DesiredGroups: desiredCapacity.Groups, Bounds: snapshot.Limits})
			if err != nil {
				return nil, fmt.Errorf("adjustment algorithm %q for target %q: %w", pipeline.AdjustmentAlgorithm.Name(), snapshot.Target.Name, err)
			}
		}
		result, err := pipeline.Resolver.Resolve(snapshot, pipeline.DecisionAlgorithm.Name(), desiredCapacity, pipeline.AdjustmentAlgorithm.Name(), adjusted)
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
