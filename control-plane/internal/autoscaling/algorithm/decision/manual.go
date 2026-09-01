// SPDX-License-Identifier: Apache-2.0
// SPDX-FileCopyrightText: Copyright contributors to the Foretoken project
package decision

import (
	"github.com/shiweijiezero/foretoken/control-plane/internal/autoscaling/algorithm"
	"github.com/shiweijiezero/foretoken/control-plane/internal/autoscaling/core"
)

type Manual struct{}

// Name identifies the manual decision algorithm for registry consumers.
func (Manual) Name() string { return "manual" }

// RecommendReplicas returns the caller-compiled baseline for fixed capacity control.
func (Manual) RecommendReplicas(snapshot core.ScalingSnapshot) (core.ReplicaRecommendation, error) {
	return core.ReplicaRecommendation{State: core.RecommendationAvailable, Replicas: snapshot.Replicas.BaselineReplicas, Reason: core.RecommendationReasonManualIntent, Message: "capacity follows ModelService replicas"}, nil
}

func init() {
	if err := algorithm.RegisterDecisionAlgorithm("manual", func(core.DecisionConfig) (core.DecisionAlgorithm, error) { return Manual{}, nil }); err != nil {
		panic(err)
	}
}
