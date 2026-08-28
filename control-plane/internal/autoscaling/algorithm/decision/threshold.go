// SPDX-License-Identifier: Apache-2.0
// SPDX-FileCopyrightText: Copyright contributors to the Foretoken project
package decision

import (
	"context"
	"fmt"
	"github.com/shiweijiezero/foretoken/control-plane/internal/autoscaling/algorithm"
	"github.com/shiweijiezero/foretoken/control-plane/internal/autoscaling/core"
)

type Threshold struct{ ScaleUpQueue int64 }

// Name identifies the threshold decision algorithm for registry consumers.
func (Threshold) Name() string { return "threshold" }

// CalculateDesiredCapacity adjusts one group when observed queue pressure crosses the configured threshold.
func (threshold Threshold) CalculateDesiredCapacity(_ context.Context, s core.ScalingSnapshot) (core.DesiredCapacity, error) {
	current := s.Capacity.RequestedGroups
	switch s.Observation.State {
	case core.ObservationUnavailable:
		return rec(current, core.DesiredCapacityInsufficientData, core.DesiredCapacityReasonObservationUnavailable, "demand observations are unavailable"), nil
	case core.ObservationStale:
		return rec(current, core.DesiredCapacityInsufficientData, core.DesiredCapacityReasonObservationStale, "demand observations are stale"), nil
	case core.ObservationFresh:
		if !s.Observation.Window.Complete {
			return rec(current, core.DesiredCapacityInsufficientData, core.DesiredCapacityReasonObservationIncomplete, "demand observation window is incomplete"), nil
		}
	default:
		return rec(current, core.DesiredCapacityInsufficientData, core.DesiredCapacityReasonObservationUnavailable, "demand observations are unavailable"), nil
	}
	if s.Observation.QueueRequests > threshold.ScaleUpQueue {
		if current < s.Limits.MaxGroups {
			return rec(current+1, core.DesiredCapacityApply, core.DesiredCapacityReasonQueuePressure, "queue pressure exceeds the configured threshold"), nil
		}
		return rec(current, core.DesiredCapacityHold, core.DesiredCapacityReasonAtMaximum, "queue pressure is high but the target is at its maximum"), nil
	}
	if s.Observation.QueueRequests == 0 && s.Observation.ActiveRequests == 0 {
		if current > s.Limits.MinGroups {
			return rec(current-1, core.DesiredCapacityApply, core.DesiredCapacityReasonIdle, "the target is idle"), nil
		}
		return rec(current, core.DesiredCapacityHold, core.DesiredCapacityReasonAtMinimum, "the target is idle but already at its minimum"), nil
	}
	return rec(current, core.DesiredCapacityHold, core.DesiredCapacityReasonStable, "current capacity matches observed demand"), nil
}
func init() {
	if err := algorithm.RegisterDecisionAlgorithm("threshold", func(config core.DecisionConfig) (core.DecisionAlgorithm, error) {
		if config.ScaleUpQueue < 0 {
			return nil, fmt.Errorf("autoscaling scaleUpQueue must be non-negative")
		}
		return Threshold{config.ScaleUpQueue}, nil
	}); err != nil {
		panic(err)
	}
}
