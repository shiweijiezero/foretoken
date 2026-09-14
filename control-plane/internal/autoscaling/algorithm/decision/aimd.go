// SPDX-License-Identifier: Apache-2.0
// SPDX-FileCopyrightText: Copyright contributors to the Foretoken project
package decision

import (
	"fmt"
	"math"

	"github.com/shiweijiezero/foretoken/control-plane/internal/autoscaling/algorithm"
	"github.com/shiweijiezero/foretoken/control-plane/internal/autoscaling/core"
)

// AIMD recommends additive growth under queue pressure and multiplicative reduction when idle.
type AIMD struct {
	AdditiveIncrease              int32
	MultiplicativeDecreasePercent int32
	ScaleUpQueuedRequests         int64
}

// Name identifies the AIMD decision algorithm for registry consumers.
func (AIMD) Name() string { return "aimd" }

// RecommendReplicas adds capacity above the queue threshold and reduces it proportionally when idle.
func (aimd AIMD) RecommendReplicas(snapshot core.ScalingSnapshot) (core.ReplicaRecommendation, error) {
	current := snapshot.Replicas.RequestedReplicas
	if snapshot.Metrics.WaitingRequests > aimd.ScaleUpQueuedRequests {
		desired := int64(current) + int64(aimd.AdditiveIncrease)
		if desired > math.MaxInt32 {
			desired = math.MaxInt32
		}
		return recommendation(int32(desired), core.RecommendationAvailable, core.RecommendationReasonQueuePressure, "queue depth exceeds the AIMD scale-up threshold"), nil
	}
	if snapshot.Metrics.WaitingRequests == 0 && snapshot.Metrics.ActiveRequests == 0 {
		desired := int32(int64(current) * int64(aimd.MultiplicativeDecreasePercent) / 100)
		return recommendation(desired, core.RecommendationAvailable, core.RecommendationReasonIdle, "the idle target is reduced by the AIMD multiplicative factor"), nil
	}
	return recommendation(current, core.RecommendationAvailable, core.RecommendationReasonStable, "request activity keeps current AIMD capacity"), nil
}

func init() {
	if err := algorithm.RegisterDecisionAlgorithm("aimd", func(config core.DecisionConfig) (core.DecisionAlgorithm, error) {
		if config.AdditiveIncrease <= 0 {
			return nil, fmt.Errorf("autoscaling AIMD additiveIncrease must be positive")
		}
		if config.MultiplicativeDecreasePercent <= 0 || config.MultiplicativeDecreasePercent >= 100 {
			return nil, fmt.Errorf("autoscaling AIMD multiplicativeDecreasePercent must be between 1 and 99")
		}
		if config.ScaleUpQueuedRequests < 0 {
			return nil, fmt.Errorf("autoscaling AIMD scaleUpQueuedRequests must not be negative")
		}
		return AIMD{
			AdditiveIncrease:              config.AdditiveIncrease,
			MultiplicativeDecreasePercent: config.MultiplicativeDecreasePercent,
			ScaleUpQueuedRequests:         config.ScaleUpQueuedRequests,
		}, nil
	}); err != nil {
		panic(err)
	}
}
