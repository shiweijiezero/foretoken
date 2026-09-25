// SPDX-License-Identifier: Apache-2.0
// SPDX-FileCopyrightText: Copyright contributors to the Foretoken project

package decision

import (
	"encoding/json"
	"fmt"
	"math"
	"strings"

	"github.com/shiweijiezero/foretoken/control-plane/internal/autoscaling/core"
)

// DynamoLoad applies Dynamo-style reactive load thresholds to one serving role.
// Queue telemetry drives aggregate, encoder, and prefill targets; decode targets
// use the model-server KV-cache utilization when it is available.
type DynamoLoad struct {
	prefillScaleUpQueuedRequests   int64
	prefillScaleDownQueuedRequests int64
	decodeScaleUpKVCacheUsage      float64
	decodeScaleDownKVCacheUsage    float64
}

const dynamoLoadName = "dynamo_load"

// Name identifies the Dynamo-compatible reactive load decision algorithm.
func (DynamoLoad) Name() string { return dynamoLoadName }

var dynamoLoadDescriptor = core.DecisionDescriptor{Name: dynamoLoadName, Factory: NewDynamoLoad}

// RecommendReplicas recommends one additional or fewer replica when the target
// crosses the role-specific load threshold. The step adjustment owns the final
// per-evaluation change and configured min/max bounds.
func (algorithm DynamoLoad) RecommendReplicas(snapshot core.ScalingSnapshot) (core.ReplicaRecommendation, error) {
	current := snapshot.Replicas.RequestedReplicas
	if snapshot.Metrics.State != core.MetricsFresh || !snapshot.Metrics.Window.Complete {
		return recommendation(current, core.RecommendationInsufficientData, core.RecommendationReasonMetricsIncomplete, "Dynamo load scaling requires a complete fresh metrics snapshot"), nil
	}

	if snapshot.Target.Role == core.RoleDecode {
		if snapshot.Metrics.KVCacheUsage == nil {
			return recommendation(current, core.RecommendationInsufficientData, core.RecommendationReasonMetricsUnavailable, "decode load scaling requires KV-cache utilization telemetry"), nil
		}
		usage := *snapshot.Metrics.KVCacheUsage
		if usage > algorithm.decodeScaleUpKVCacheUsage {
			return recommendation(increment(current), core.RecommendationAvailable, core.RecommendationReasonLoadPressure, "decode KV-cache utilization exceeds the Dynamo load threshold"), nil
		}
		if usage < algorithm.decodeScaleDownKVCacheUsage && snapshot.Metrics.WaitingRequests == 0 && snapshot.Metrics.ActiveRequests == 0 {
			return recommendation(decrement(current), core.RecommendationAvailable, core.RecommendationReasonLoadBelowTarget, "decode KV-cache utilization is below the Dynamo load threshold"), nil
		}
		return recommendation(current, core.RecommendationAvailable, core.RecommendationReasonStable, "decode load remains within the Dynamo thresholds"), nil
	}

	if snapshot.Metrics.WaitingRequests > algorithm.prefillScaleUpQueuedRequests {
		return recommendation(increment(current), core.RecommendationAvailable, core.RecommendationReasonLoadPressure, "queued requests exceed the Dynamo load threshold"), nil
	}
	if snapshot.Metrics.WaitingRequests <= algorithm.prefillScaleDownQueuedRequests && snapshot.Metrics.ActiveRequests == 0 {
		return recommendation(decrement(current), core.RecommendationAvailable, core.RecommendationReasonLoadBelowTarget, "queued requests are below the Dynamo load threshold"), nil
	}
	return recommendation(current, core.RecommendationAvailable, core.RecommendationReasonStable, "request load remains within the Dynamo thresholds"), nil
}

func increment(current int32) int32 {
	if current == math.MaxInt32 {
		return current
	}
	return current + 1
}

func decrement(current int32) int32 {
	if current <= 0 {
		return 0
	}
	return current - 1
}

// NewDynamoLoad decodes the optional role thresholds and owns their defaults.
// The CRD only carries an opaque parameters object; adding this algorithm does
// not require a schema change.
func NewDynamoLoad(parameters json.RawMessage) (core.DecisionAlgorithm, error) {
	mode := "throughput"
	upQueue, downQueue := int64(1), int64(0)
	upKV, downKV := 0.8, 0.6
	if err := core.DecodeParameters(parameters, map[string]any{
		"mode":                           &mode,
		"prefillScaleUpQueuedRequests":   &upQueue,
		"prefillScaleDownQueuedRequests": &downQueue,
		"decodeScaleUpKVCacheUsage":      &upKV,
		"decodeScaleDownKVCacheUsage":    &downKV,
	}); err != nil {
		return nil, err
	}
	mode = strings.ToLower(strings.TrimSpace(mode))
	if mode != "throughput" && mode != "latency" {
		return nil, fmt.Errorf("autoscaling dynamo_load mode must be throughput or latency")
	}
	var supplied map[string]json.RawMessage
	if len(parameters) != 0 {
		if err := json.Unmarshal(parameters, &supplied); err != nil {
			return nil, err
		}
	}
	if mode == "latency" {
		if _, ok := supplied["decodeScaleUpKVCacheUsage"]; !ok {
			upKV = 0.4
		}
		if _, ok := supplied["decodeScaleDownKVCacheUsage"]; !ok {
			downKV = 0.1
		}
	}
	if upQueue < 0 || downQueue < 0 || downQueue > upQueue {
		return nil, fmt.Errorf("autoscaling dynamo_load queue thresholds are invalid")
	}
	if upKV < 0 || upKV > 1 || downKV < 0 || downKV > 1 || downKV > upKV {
		return nil, fmt.Errorf("autoscaling dynamo_load KV-cache thresholds must be between 0 and 1 with scale-down below scale-up")
	}
	return DynamoLoad{
		prefillScaleUpQueuedRequests:   upQueue,
		prefillScaleDownQueuedRequests: downQueue,
		decodeScaleUpKVCacheUsage:      upKV,
		decodeScaleDownKVCacheUsage:    downKV,
	}, nil
}
