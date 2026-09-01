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
