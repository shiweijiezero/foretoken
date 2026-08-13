// SPDX-License-Identifier: Apache-2.0
// SPDX-FileCopyrightText: Copyright contributors to the Foretoken project
package decision

import (
	"context"
	"fmt"
	"github.com/shiweijiezero/foretoken/control-plane/internal/autoscaling/algorithm"
	"github.com/shiweijiezero/foretoken/control-plane/internal/autoscaling/core"
)

type Queue struct{ TargetQueuePerRoutableGroup int64 }

func (Queue) Name() string { return "queue" }
func (queue Queue) CalculateDesiredCapacity(_ context.Context, s core.ScalingSnapshot) (core.DesiredCapacity, error) {
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
	supply := s.Capacity.RoutableGroups
	if supply < 1 {
		supply = 1
	}
	if exceeds(s.Observation.QueueRequests, queue.TargetQueuePerRoutableGroup, supply) {
		if current < s.Limits.MaxGroups {
			return rec(current+1, core.DesiredCapacityApply, core.DesiredCapacityReasonQueuePressure, "queue pressure exceeds routable capacity"), nil
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
func exceeds(requests, target int64, supply int32) bool {
	if requests <= 0 {
		return false
	}
	if target <= 0 {
		return true
	}
	groups := int64(supply)
	q, r := requests/groups, requests%groups
	return q > target || q == target && r > 0
}
func rec(desired int32, d core.DesiredCapacityDisposition, r core.DesiredCapacityReason, m string) core.DesiredCapacity {
	return core.DesiredCapacity{Disposition: d, Groups: desired, Reason: r, Message: m}
}
func init() {
	if err := algorithm.RegisterDecisionAlgorithm("queue", func(config core.DecisionConfig) (core.DecisionAlgorithm, error) {
		if config.TargetQueuePerRoutableGroup < 0 {
			return nil, fmt.Errorf("autoscaling targetQueuePerRoutableGroup must be non-negative")
		}
		return Queue{config.TargetQueuePerRoutableGroup}, nil
	}); err != nil {
		panic(err)
	}
}
