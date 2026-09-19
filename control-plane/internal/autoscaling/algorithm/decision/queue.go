// SPDX-License-Identifier: Apache-2.0
// SPDX-FileCopyrightText: Copyright contributors to the Foretoken project
package decision

import (
	"encoding/json"
	"fmt"
	"math"

	"github.com/shiweijiezero/foretoken/control-plane/internal/autoscaling/core"
)

type Queue struct{ TargetAverageQueuedRequests int64 }

// Name identifies the queue decision algorithm for registry consumers.
func (Queue) Name() string { return "queue" }

// RecommendReplicas converts aggregate waiting requests into an HPA-style average-value recommendation.
func (queue Queue) RecommendReplicas(snapshot core.ScalingSnapshot) (core.ReplicaRecommendation, error) {
	current := snapshot.Replicas.RequestedReplicas
	requests := snapshot.Metrics.WaitingRequests
	if requests > 0 {
		desired := divideRoundUp(requests, queue.TargetAverageQueuedRequests)
		if desired > current {
			return recommendation(desired, core.RecommendationAvailable, core.RecommendationReasonQueuePressure, "queued requests exceed target average capacity"), nil
		}
		return recommendation(desired, core.RecommendationAvailable, core.RecommendationReasonQueueBelowTarget, "queued requests require lower average-value capacity"), nil
	}
	if snapshot.Metrics.ActiveRequests == 0 {
		return recommendation(0, core.RecommendationAvailable, core.RecommendationReasonIdle, "the target is idle"), nil
	}
	return recommendation(current, core.RecommendationAvailable, core.RecommendationReasonStable, "active requests keep current capacity"), nil
}

func divideRoundUp(value, divisor int64) int32 {
	replicas := value / divisor
	if value%divisor != 0 {
		replicas++
	}
	if replicas > math.MaxInt32 {
		return math.MaxInt32
	}
	return int32(replicas)
}

func recommendation(replicas int32, state core.RecommendationState, reason core.RecommendationReason, message string) core.ReplicaRecommendation {
	return core.ReplicaRecommendation{State: state, Replicas: replicas, Reason: reason, Message: message}
}

// NewQueue constructs the queue policy for the built-in registry and validates its capacity target.
func NewQueue(parameters json.RawMessage) (core.DecisionAlgorithm, error) {
	target := int64(1)
	if err := core.DecodeParameters(parameters, map[string]any{"targetAverageQueuedRequests": &target}); err != nil {
		return nil, err
	}
	if target <= 0 {
		return nil, fmt.Errorf("autoscaling targetAverageQueuedRequests must be positive")
	}
	return Queue{TargetAverageQueuedRequests: target}, nil
}
