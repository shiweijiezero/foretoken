// SPDX-License-Identifier: Apache-2.0
// SPDX-FileCopyrightText: Copyright contributors to the Foretoken project
package decision

import (
	"encoding/json"
	"fmt"
	"math"

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

// NewAIMD decodes policy parameters for the registry and returns an additive-growth, idle-reduction policy.
func NewAIMD(parameters json.RawMessage) (core.DecisionAlgorithm, error) {
	increase, retainedPercent, threshold := int64(1), int64(50), int64(0)
	if err := core.DecodeParameters(parameters, map[string]any{
		"additiveIncrease":              &increase,
		"multiplicativeDecreasePercent": &retainedPercent,
		"scaleUpQueuedRequests":         &threshold,
	}); err != nil {
		return nil, err
	}
	if increase <= 0 || increase > math.MaxInt32 {
		return nil, fmt.Errorf("autoscaling AIMD additiveIncrease must be between 1 and %d", math.MaxInt32)
	}
	if retainedPercent <= 0 || retainedPercent >= 100 {
		return nil, fmt.Errorf("autoscaling AIMD multiplicativeDecreasePercent must be between 1 and 99")
	}
	if threshold < 0 {
		return nil, fmt.Errorf("autoscaling AIMD scaleUpQueuedRequests must not be negative")
	}
	return AIMD{
		AdditiveIncrease:              int32(increase),
		MultiplicativeDecreasePercent: int32(retainedPercent),
		ScaleUpQueuedRequests:         threshold,
	}, nil
}
