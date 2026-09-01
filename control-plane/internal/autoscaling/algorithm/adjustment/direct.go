// SPDX-License-Identifier: Apache-2.0
// SPDX-FileCopyrightText: Copyright contributors to the Foretoken project
package adjustment

import (
	"github.com/shiweijiezero/foretoken/control-plane/internal/autoscaling/algorithm"
	"github.com/shiweijiezero/foretoken/control-plane/internal/autoscaling/core"
)

type Direct struct{}

// Name identifies the direct adjustment algorithm for registry consumers.
func (Direct) Name() string { return "direct" }

// Adjust clamps the replica recommendation to the configured bounds.
func (Direct) Adjust(input core.AdjustmentInput) (core.ReplicaAdjustment, error) {
	return core.ReplicaAdjustment{Replicas: clip(input.RecommendedReplicas, input.Limits.MinReplicas, input.Limits.MaxReplicas), Reason: core.AdjustmentReasonDirect, Message: "replica recommendation is applied directly"}, nil
}

func init() {
	if err := algorithm.RegisterAdjustmentAlgorithm("direct", func(core.AdjustmentConfig) (core.AdjustmentAlgorithm, error) { return Direct{}, nil }); err != nil {
		panic(err)
	}
}
