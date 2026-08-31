// SPDX-License-Identifier: Apache-2.0
// SPDX-FileCopyrightText: Copyright contributors to the Foretoken project
package decision

import (
	"fmt"
	"math"

	"github.com/shiweijiezero/foretoken/control-plane/internal/autoscaling/algorithm"
	"github.com/shiweijiezero/foretoken/control-plane/internal/autoscaling/core"
)

type QueueThreshold struct {
	ScaleUpQueuedRequests   int64
	ScaleDownQueuedRequests int64
}

// Name identifies the absolute queue-threshold decision algorithm for registry consumers.
func (QueueThreshold) Name() string { return "queue_threshold" }

// CalculateDesiredCapacity recommends one Group when aggregate queue depth crosses configured boundaries.
func (threshold QueueThreshold) CalculateDesiredCapacity(snapshot core.ScalingSnapshot) (core.DesiredCapacity, error) {
	current := snapshot.Capacity.RequestedGroups
	if snapshot.Observation.QueueRequests > threshold.ScaleUpQueuedRequests {
		desired := current
		if desired < math.MaxInt32 {
			desired++
		}
		return recommendation(desired, core.DesiredCapacityApply, core.DesiredCapacityReasonQueuePressure, "queue depth exceeds the scale-up threshold"), nil
	}
	if snapshot.Observation.QueueRequests <= threshold.ScaleDownQueuedRequests && snapshot.Observation.ActiveRequests == 0 {
		desired := current - 1
		if desired < 0 {
			desired = 0
		}
		return recommendation(desired, core.DesiredCapacityApply, core.DesiredCapacityReasonIdle, "queue depth is within the idle threshold"), nil
	}
	return recommendation(current, core.DesiredCapacityApply, core.DesiredCapacityReasonStable, "queue depth remains between scaling thresholds"), nil
}

func init() {
	if err := algorithm.RegisterDecisionAlgorithm("queue_threshold", func(config core.DecisionConfig) (core.DecisionAlgorithm, error) {
		if config.ScaleUpQueuedRequests < 0 || config.ScaleDownQueuedRequests < 0 || config.ScaleDownQueuedRequests > config.ScaleUpQueuedRequests {
			return nil, fmt.Errorf("autoscaling queue thresholds are invalid")
		}
		return QueueThreshold{ScaleUpQueuedRequests: config.ScaleUpQueuedRequests, ScaleDownQueuedRequests: config.ScaleDownQueuedRequests}, nil
	}); err != nil {
		panic(err)
	}
}
