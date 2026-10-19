// SPDX-License-Identifier: Apache-2.0
// SPDX-FileCopyrightText: Copyright contributors to the Foretoken project

// Recommends Pool capacity from per-engine token demand and available KV capacity.
package decision

import (
	"encoding/json"
	"fmt"
	"math"

	"github.com/shiweijiezero/foretoken/control-plane/internal/autoscaling/core"
)

const tokenLoadName = "token_load"

// TokenLoad evaluates scheduler token demand for prefill, decode, and aggregate Pools.
// Adjustment and controller lifecycle rules determine the capacity actually applied.
type TokenLoad struct {
	mode                   string
	prefillUp, prefillDown float64
	decodeUp, decodeDown   float64
}

var tokenLoadDescriptor = core.DecisionDescriptor{Name: tokenLoadName, Factory: NewTokenLoad}

// NewTokenLoad constructs a preset or explicit-threshold policy for the built-in registry.
func NewTokenLoad(parameters json.RawMessage) (core.DecisionAlgorithm, error) {
	mode := "throughput"
	var prefillUp, prefillDown *uint64
	var decodeUp, decodeDown *float64
	if err := core.DecodeParameters(parameters, map[string]any{
		"mode":                        &mode,
		"prefillScaleUpQueueTokens":   &prefillUp,
		"prefillScaleDownQueueTokens": &prefillDown,
		"decodeScaleUpTokenRatio":     &decodeUp,
		"decodeScaleDownTokenRatio":   &decodeDown,
	}); err != nil {
		return nil, err
	}
	policy := TokenLoad{mode: mode, prefillUp: 1, prefillDown: 0.1, decodeUp: 1, decodeDown: 0.6}
	switch mode {
	case "throughput", "latency":
		if prefillUp != nil || prefillDown != nil || decodeUp != nil || decodeDown != nil {
			return nil, fmt.Errorf("token_load custom thresholds require mode load")
		}
		if mode == "latency" {
			policy.prefillUp, policy.prefillDown, policy.decodeUp, policy.decodeDown = 0.1, 0, 0.4, 0.1
		}
	case "load":
		if prefillUp == nil || prefillDown == nil || decodeUp == nil || decodeDown == nil {
			return nil, fmt.Errorf("token_load mode load requires all four token thresholds")
		}
		if *prefillUp <= *prefillDown || !(*decodeDown >= 0 && *decodeUp <= 1 && *decodeUp > *decodeDown) {
			return nil, fmt.Errorf("token_load scale-up thresholds must exceed scale-down thresholds; token ratios must be in [0, 1]")
		}
		policy.prefillUp, policy.prefillDown = float64(*prefillUp), float64(*prefillDown)
		policy.decodeUp, policy.decodeDown = *decodeUp, *decodeDown
	default:
		return nil, fmt.Errorf("token_load mode must be throughput, latency, or load")
	}
	return policy, nil
}

// RecommendReplicas evaluates each engine and returns a one-Group capacity recommendation.
// Scale-down checks redistributed demand; request draining stays with the Group controller.
func (algorithm TokenLoad) RecommendReplicas(snapshot core.ScalingSnapshot) (core.ReplicaRecommendation, error) {
	current := snapshot.Replicas.RequestedReplicas
	if snapshot.Target.Role == core.RoleEncoder {
		return recommendation(current, core.RecommendationInsufficientData, core.RecommendationReasonMetricsUnavailable, "token load does not define an encoder policy"), nil
	}
	if len(snapshot.Metrics.EngineLoads) == 0 {
		return recommendation(current, core.RecommendationInsufficientData, core.RecommendationReasonMetricsUnavailable, "token load requires scheduler token observations"), nil
	}
	up, down := algorithm.decodeUp, algorithm.decodeDown
	if snapshot.Target.Role == core.RolePrefill {
		up, down = algorithm.prefillUp, algorithm.prefillDown
	}
	maximum := 0.0
	allBelow := true
	for _, engine := range snapshot.Metrics.EngineLoads {
		var value float64
		if snapshot.Target.Role == core.RolePrefill {
			value = float64(engine.QueuedPrefillTokens)
			if algorithm.mode != "load" {
				value /= float64(engine.ContextLength)
			}
		} else {
			// Add in floating point so independent uint64 counts cannot overflow.
			value = float64(engine.ScheduledDecodeKVTokens) + float64(engine.QueuedDecodeKVTokens)
			if snapshot.Target.Role == core.RoleAggregate {
				value += float64(engine.QueuedPrefillTokens)
			}
			value /= float64(engine.KVCapacityTokens)
		}
		maximum = math.Max(maximum, value)
		inclusiveUp := snapshot.Target.Role == core.RolePrefill || algorithm.mode == "load"
		if value > up || inclusiveUp && value == up {
			desired := current
			if desired < math.MaxInt32 {
				desired++
			}
			return recommendation(desired, core.RecommendationAvailable, core.RecommendationReasonLoadPressure, fmt.Sprintf("Group %s DP rank %d exceeds its token-load threshold", engine.GroupUID, engine.DataParallelRank)), nil
		}
		inclusiveDown := algorithm.mode == "load" || snapshot.Target.Role == core.RolePrefill && algorithm.mode == "latency"
		allBelow = allBelow && (value < down || inclusiveDown && value == down)
	}
	// Estimate the highest engine load after one Group's demand is redistributed.
	if current > 1 && allBelow && maximum*float64(current)/float64(current-1) < up {
		return recommendation(current-1, core.RecommendationAvailable, core.RecommendationReasonLoadBelowTarget, "all engines remain below saturation after consolidation"), nil
	}
	return recommendation(current, core.RecommendationAvailable, core.RecommendationReasonStable, "engine token load keeps current capacity"), nil
}

// Name identifies the token-load algorithm for configuration and status consumers.
func (TokenLoad) Name() string { return tokenLoadName }

// RequiresEngineLoad tells the controller which targets need scheduler token observations.
func (TokenLoad) RequiresEngineLoad(role core.TargetRole) bool {
	return role != core.RoleEncoder
}
