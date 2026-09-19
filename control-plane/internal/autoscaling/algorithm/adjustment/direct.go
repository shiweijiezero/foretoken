// SPDX-License-Identifier: Apache-2.0
// SPDX-FileCopyrightText: Copyright contributors to the Foretoken project
package adjustment

import (
	"encoding/json"

	"github.com/shiweijiezero/foretoken/control-plane/internal/autoscaling/core"
)

type Direct struct{}

// Name identifies the direct adjustment algorithm for registry consumers.
func (Direct) Name() string { return "direct" }

// Adjust clamps the replica recommendation to the configured bounds.
func (Direct) Adjust(input core.AdjustmentInput) (core.ReplicaAdjustment, error) {
	return core.ReplicaAdjustment{Replicas: clip(input.RecommendedReplicas, input.Limits.MinReplicas, input.Limits.MaxReplicas), Reason: core.AdjustmentReasonDirect, Message: "replica recommendation is applied directly"}, nil
}

// NewDirect constructs immediate bounded adjustment for the registry and rejects unused parameters.
func NewDirect(parameters json.RawMessage, _ *core.RecommendationHistory) (core.AdjustmentAlgorithm, error) {
	if err := core.DecodeParameters(parameters, nil); err != nil {
		return nil, err
	}
	return Direct{}, nil
}
