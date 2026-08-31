// SPDX-License-Identifier: Apache-2.0
// SPDX-FileCopyrightText: Copyright contributors to the Foretoken project
package decision

import (
	"fmt"
	"math"

	"github.com/shiweijiezero/foretoken/control-plane/internal/autoscaling/algorithm"
	"github.com/shiweijiezero/foretoken/control-plane/internal/autoscaling/core"
)

type Queue struct{ TargetAverageQueuedRequests int64 }

// Name identifies the queue decision algorithm for registry consumers.
func (Queue) Name() string { return "queue" }

// CalculateDesiredCapacity converts aggregate waiting requests into an HPA-style average-value recommendation.
func (queue Queue) CalculateDesiredCapacity(snapshot core.ScalingSnapshot) (core.DesiredCapacity, error) {
	current := snapshot.Capacity.RequestedGroups
	requests := snapshot.Observation.QueueRequests
	if requests > 0 {
		desired := divideRoundUp(requests, queue.TargetAverageQueuedRequests)
		if desired > current {
			return recommendation(desired, core.DesiredCapacityApply, core.DesiredCapacityReasonQueuePressure, "queued requests exceed target average capacity"), nil
		}
		return recommendation(desired, core.DesiredCapacityApply, core.DesiredCapacityReasonQueueBelowTarget, "queued requests require lower average-value capacity"), nil
	}
	if snapshot.Observation.ActiveRequests == 0 {
		return recommendation(0, core.DesiredCapacityApply, core.DesiredCapacityReasonIdle, "the target is idle"), nil
	}
	return recommendation(current, core.DesiredCapacityApply, core.DesiredCapacityReasonStable, "active requests keep current capacity"), nil
}

func divideRoundUp(value, divisor int64) int32 {
	groups := value / divisor
	if value%divisor != 0 {
		groups++
	}
	if groups > math.MaxInt32 {
		return math.MaxInt32
	}
	return int32(groups)
}

func recommendation(groups int32, disposition core.DesiredCapacityDisposition, reason core.DesiredCapacityReason, message string) core.DesiredCapacity {
	return core.DesiredCapacity{Disposition: disposition, Groups: groups, Reason: reason, Message: message}
}

func init() {
	if err := algorithm.RegisterDecisionAlgorithm("queue", func(config core.DecisionConfig) (core.DecisionAlgorithm, error) {
		if config.TargetAverageQueuedRequests <= 0 {
			return nil, fmt.Errorf("autoscaling targetAverageQueuedRequests must be positive")
		}
		return Queue{TargetAverageQueuedRequests: config.TargetAverageQueuedRequests}, nil
	}); err != nil {
		panic(err)
	}
}
