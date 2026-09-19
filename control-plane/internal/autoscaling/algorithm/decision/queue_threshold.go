// SPDX-License-Identifier: Apache-2.0
// SPDX-FileCopyrightText: Copyright contributors to the Foretoken project
package decision

import (
	"encoding/json"
	"fmt"
	"math"

	"github.com/shiweijiezero/foretoken/control-plane/internal/autoscaling/core"
)

type QueueThreshold struct {
	ScaleUpQueuedRequests   int64
	ScaleDownQueuedRequests int64
}

// Name identifies the absolute queue-threshold decision algorithm for registry consumers.
func (QueueThreshold) Name() string { return "queue_threshold" }

// RecommendReplicas changes the recommendation by one replica when aggregate queue depth crosses configured boundaries.
func (threshold QueueThreshold) RecommendReplicas(snapshot core.ScalingSnapshot) (core.ReplicaRecommendation, error) {
	current := snapshot.Replicas.RequestedReplicas
	if snapshot.Metrics.WaitingRequests > threshold.ScaleUpQueuedRequests {
		desired := current
		if desired < math.MaxInt32 {
			desired++
		}
		return recommendation(desired, core.RecommendationAvailable, core.RecommendationReasonQueuePressure, "queue depth exceeds the scale-up threshold"), nil
	}
	if snapshot.Metrics.WaitingRequests <= threshold.ScaleDownQueuedRequests && snapshot.Metrics.ActiveRequests == 0 {
		desired := current - 1
		if desired < 0 {
			desired = 0
		}
		return recommendation(desired, core.RecommendationAvailable, core.RecommendationReasonIdle, "queue depth is within the idle threshold"), nil
	}
	return recommendation(current, core.RecommendationAvailable, core.RecommendationReasonStable, "queue depth remains between scaling thresholds"), nil
}

// NewQueueThreshold constructs the backlog policy for the built-in registry and validates its boundaries.
func NewQueueThreshold(parameters json.RawMessage) (core.DecisionAlgorithm, error) {
	scaleUp, scaleDown := int64(1), int64(0)
	if err := core.DecodeParameters(parameters, map[string]any{
		"scaleUpQueuedRequests":   &scaleUp,
		"scaleDownQueuedRequests": &scaleDown,
	}); err != nil {
		return nil, err
	}
	if scaleUp < 0 || scaleDown < 0 || scaleDown > scaleUp {
		return nil, fmt.Errorf("autoscaling queue thresholds are invalid")
	}
	return QueueThreshold{ScaleUpQueuedRequests: scaleUp, ScaleDownQueuedRequests: scaleDown}, nil
}
