// SPDX-License-Identifier: Apache-2.0
// SPDX-FileCopyrightText: Copyright contributors to the Foretoken project

// Preserves the caller's baseline replica count as fixed capacity intent.
package decision

import (
	"encoding/json"

	"github.com/shiweijiezero/foretoken/control-plane/internal/autoscaling/core"
)

type Manual struct{}

const manualName = "manual"

var manualDescriptor = core.DecisionDescriptor{Name: manualName, Factory: NewManual}

// NewManual constructs fixed-capacity control for the registry and rejects unused parameters.
func NewManual(parameters json.RawMessage) (core.DecisionAlgorithm, error) {
	if err := core.DecodeParameters(parameters, nil); err != nil {
		return nil, err
	}
	return Manual{}, nil
}

// RecommendReplicas returns the caller-compiled baseline for fixed capacity control.
func (Manual) RecommendReplicas(snapshot core.ScalingSnapshot) (core.ReplicaRecommendation, error) {
	return core.ReplicaRecommendation{State: core.RecommendationAvailable, Replicas: snapshot.Replicas.BaselineReplicas, Reason: core.RecommendationReasonManualIntent, Message: "capacity follows ModelService replicas"}, nil
}

// Name identifies the manual decision algorithm for registry consumers.
func (Manual) Name() string { return manualName }
